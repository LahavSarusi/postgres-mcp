"""Shared fixtures.

Tests that need PostgreSQL use the `pg` fixture. They run against a scratch
database created for the session and dropped afterwards, on:
  - the server in TEST_DATABASE_URL, when set (any database URL on it with
    rights to CREATE DATABASE), or else
  - a throwaway postgres container started with Docker.
With neither available those tests are skipped, with the reason shown.

The LLM is replaced by `MockLlm`, a local HTTP server speaking the
OpenAI-compatible chat-completions API with scripted replies.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

# Every setting the project reads, fixed before postgres_mcp is imported, so the
# developer's own .env (which never overrides set variables) cannot leak in.
# PG* stay empty until a test asks for the database.
BASE_ENV = {
    "PGHOST": "",
    "PGPORT": "",
    "PGUSER": "",
    "PGPASSWORD": "",
    "PGDATABASE": "",
    "PGSSLMODE": "",
    "PGCONNECT_TIMEOUT": "",
    "LLM_BASE_URL": "http://127.0.0.1:9/v1",
    "LLM_API_KEY": "test-key",
    "LLM_MODEL": "mock-model",
    "MAX_ROWS": "1000",
    "QUERY_TIMEOUT_MS": "2000",
}
os.environ.update(BASE_ENV)

import psycopg  # noqa: E402
import pytest  # noqa: E402
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402

from postgres_mcp import db, descriptions, init_descriptions, schema  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(sys.executable).parent  # the venv's Scripts/ or bin/

SEED_SQL = r"""
CREATE TABLE customers (
  id serial PRIMARY KEY,
  name text NOT NULL,
  city text,
  status char(1) NOT NULL DEFAULT 'A'
);
CREATE SCHEMA sales;
CREATE TABLE sales.orders (
  id bigserial PRIMARY KEY,
  customer_id int REFERENCES customers(id),
  total numeric(12,2),
  ordered_at timestamptz DEFAULT '2026-11-01 10:00:00+00',
  ship_date date,
  meta jsonb,
  blob bytea,
  token uuid,
  ratio double precision,
  "Weird Col" text
);
CREATE TABLE sales.order_items (
  order_id bigint REFERENCES sales.orders(id),
  line int,
  qty int,
  PRIMARY KEY (order_id, line)
);
CREATE TABLE "Mixed Case" (k int PRIMARY KEY, v text);
CREATE TABLE types_demo (
  i interval, ip inet, r int4range, t time, arr int[], flag boolean, big bigint, f real
);
CREATE VIEW active_customers AS SELECT * FROM customers WHERE status = 'A';
INSERT INTO customers (name, city, status) VALUES
  ('דנה כהן', 'חיפה', 'A'), ('Bob', 'Haifa', 'C'), ('Ann 100%', 'Tel Aviv', 'A');
INSERT INTO sales.orders (customer_id, total, ship_date, meta, blob, token, ratio, "Weird Col")
VALUES
  (1, 1234567890.12, '2026-11-01', '{"a":[1,2],"h":"שלום"}', '\xdeadbeef',
   '6f1c2b8e-1d2a-4c55-9a8e-0b1f2a3c4d5e', 'NaN', 'x'),
  (2, 10.50, NULL, NULL, NULL, NULL, 0.25, NULL);
INSERT INTO sales.order_items VALUES (1, 1, 2), (1, 2, 1);
INSERT INTO "Mixed Case" SELECT g, 'v' || g FROM generate_series(1, 1200) g;
INSERT INTO types_demo VALUES
  ('1 day 02:00', '10.0.0.1', '[1,5)', '13:45:00', '{1,2,3}', true, 9007199254740993, 'Infinity');
"""


# ---------------------------------------------------------------- isolation


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Fresh module state per test, and a descriptions.json path in tmp_path.

    The project's real descriptions.json is never read or written by tests.
    """
    notes = tmp_path / "descriptions.json"
    for module in (descriptions, schema, init_descriptions):
        monkeypatch.setattr(module, "descriptions_file", notes)
    monkeypatch.setattr(db, "_conninfo", None)
    monkeypatch.setattr(db, "_connect_options", {})
    monkeypatch.setattr(schema, "_cached", None)
    for name, value in BASE_ENV.items():
        monkeypatch.setenv(name, value)
    return notes


# ---------------------------------------------------------------- PostgreSQL


@dataclass
class Database:
    """Connection details of the scratch database."""

    host: str
    port: str
    user: str
    password: str
    dbname: str
    container: str | None  # set when the server runs in a container we started

    @property
    def url(self) -> str:
        """The database as a postgresql:// URL, with user and password percent-encoded."""
        return (
            f"postgresql://{quote(self.user, safe='')}:{quote(self.password, safe='')}"
            f"@{self.host}:{self.port}/{quote(self.dbname, safe='')}"
        )

    @property
    def env(self) -> dict[str, str]:
        """The PG* variables that point libpq at the database."""
        return {
            "PGHOST": self.host,
            "PGPORT": self.port,
            "PGUSER": self.user,
            "PGPASSWORD": self.password,
            "PGDATABASE": self.dbname,
        }


def direct_connect(url: str, **options: Any) -> psycopg.Connection[tuple[Any, ...]]:
    """A plain psycopg connection for test setup and checks.

    The empty PGSSLMODE / PGCONNECT_TIMEOUT in BASE_ENV (there to block .env)
    would be rejected by libpq, so they are given unless the URL sets them.
    """
    settings = conninfo_to_dict(url)
    if "sslmode" not in settings:
        options.setdefault("sslmode", "prefer")
    if "connect_timeout" not in settings:
        options.setdefault("connect_timeout", 10)
    return psycopg.connect(url, **options)


def _wait_for(url: str, seconds: float) -> None:
    """Retries connecting to `url` until it works or `seconds` have passed."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            with direct_connect(url, connect_timeout=2):
                return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


@pytest.fixture(scope="session")
def pg_server() -> Iterator[tuple[str, str | None]]:
    """(server URL, container id or None) for the session."""
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url, None
        return
    if not shutil.which("docker"):
        pytest.skip("needs PostgreSQL: set TEST_DATABASE_URL or install Docker")
    started = subprocess.run(
        [
            "docker", "run", "-d", "--rm", "-e", "POSTGRES_PASSWORD=testpw",
            "-p", "127.0.0.1::5432", "postgres:17-alpine",
        ],
        capture_output=True, text=True, timeout=600,
    )  # fmt: skip
    if started.returncode != 0:
        pytest.skip(f"needs PostgreSQL: docker run failed: {started.stderr.strip()}")
    container = started.stdout.strip()
    try:
        mapping = subprocess.run(
            ["docker", "port", container, "5432/tcp"],
            capture_output=True, text=True, check=True, timeout=60,
        ).stdout.split()[0]  # fmt: skip
        port = mapping.rsplit(":", 1)[1]
        url = f"postgresql://postgres:testpw@127.0.0.1:{port}/postgres"
        _wait_for(url, 90)
        yield url, container
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=120)


@pytest.fixture(scope="session")
def database(pg_server: tuple[str, str | None]) -> Iterator[Database]:
    """A scratch database with the seed schema, dropped after the session."""
    server_url, container = pg_server
    name = f"pmcp_test_{uuid.uuid4().hex[:10]}"
    with direct_connect(server_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    info = conninfo_to_dict(make_conninfo(server_url, dbname=name))
    target = Database(
        host=str(info.get("host") or "localhost"),
        port=str(info.get("port") or "5432"),
        user=str(info.get("user") or ""),
        password=str(info.get("password") or ""),
        dbname=name,
        container=container,
    )
    try:
        with direct_connect(target.url, autocommit=True) as conn:
            conn.execute(SEED_SQL)
        yield target
    finally:
        with direct_connect(server_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def pg(database: Database, monkeypatch: pytest.MonkeyPatch) -> Database:
    """The scratch database, with PG* pointing at it (db.connect() not yet called)."""
    for name, value in database.env.items():
        monkeypatch.setenv(name, value)
    return database


@pytest.fixture
def connected(pg: Database) -> Database:
    """The scratch database, with db.connect() done."""
    db.connect()
    return pg


# ---------------------------------------------------------------- mock LLM


@dataclass
class Reply:
    """One scripted HTTP reply from the mock LLM."""

    content: Any = None  # becomes choices[0].message.content
    status: int = 200
    raw: bytes | None = None  # sent as-is instead of a JSON completion
    delay: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)


def sql_reply(sql: str) -> Reply:
    """A reply whose message content is `sql`."""
    return Reply(content=sql)


class MockLlm:
    """A local OpenAI-compatible chat-completions server with scripted replies.

    Each POST is recorded in `requests` and answered with the next reply in `replies`; when none are
    left it answers 500 "no reply".
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.replies: list[Reply] = []
        mock = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                mock.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": json.loads(raw) if raw else None,
                    }
                )
                reply = mock.replies.pop(0) if mock.replies else Reply(status=500, raw=b"no reply")
                if reply.delay:
                    time.sleep(reply.delay)
                if reply.raw is not None:
                    data = reply.raw
                else:
                    data = json.dumps(
                        {"choices": [{"message": {"content": reply.content}}]}
                    ).encode()
                self.send_response(reply.status)
                for key, value in {"Content-Type": "application/json", **reply.headers}.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        """The URL to use as LLM_BASE_URL."""
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def script(self, *replies: Reply | str) -> None:
        """Queues replies for the next requests; a string becomes an SQL reply."""
        self.replies = [sql_reply(r) if isinstance(r, str) else r for r in replies]

    @property
    def questions(self) -> list[str]:
        """The last message of every request received."""
        return [r["body"]["messages"][-1]["content"] for r in self.requests]


@pytest.fixture(scope="session")
def llm_server() -> Iterator[MockLlm]:
    """One mock LLM server for the whole session."""
    mock = MockLlm()
    yield mock
    mock.server.shutdown()


@pytest.fixture
def llm(llm_server: MockLlm, monkeypatch: pytest.MonkeyPatch) -> MockLlm:
    """The mock LLM, reset, with LLM_BASE_URL pointing at it."""
    llm_server.requests.clear()
    llm_server.replies.clear()
    monkeypatch.setenv("LLM_BASE_URL", llm_server.base_url)
    return llm_server


# ---------------------------------------------------------------- subprocesses


def script_path(name: str) -> str:
    """Path of an installed console script (postgres-mcp-*)."""
    found = shutil.which(name, path=str(SCRIPTS))
    assert found, f"{name} is not installed; run: pip install -e .[dev]"
    return found


def run_script(
    args: list[str], env: dict[str, str] | None = None, timeout: float = 60
) -> subprocess.CompletedProcess[bytes]:
    """Runs a command with the current (isolated) environment plus `env`."""
    full_env = {**os.environ, **(env or {})}
    full_env.pop("PYTHONIOENCODING", None)
    return subprocess.run(args, env=full_env, capture_output=True, timeout=timeout)


# ---------------------------------------------------------------- options


def pytest_addoption(parser: pytest.Parser) -> None:
    """Adds --run-image, which turns on the slow Docker image tests."""
    parser.addoption(
        "--run-image", action="store_true", help="also build and test the Docker image"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skips tests marked `image` unless --run-image is given."""
    if config.getoption("--run-image"):
        return
    skip = pytest.mark.skip(reason="Docker image test; enable with --run-image")
    for item in items:
        if "image" in item.keywords:
            item.add_marker(skip)
