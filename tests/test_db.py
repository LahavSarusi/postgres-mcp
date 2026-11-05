"""db.py: connection settings, and the read-only guards against a real database."""

import json
import os
import time

import psycopg
import pytest
from conftest import Database, direct_connect

from postgres_mcp import db
from postgres_mcp.output import run, to_json

# ---------------------------------------------------------------- connect()


def test_connect_names_missing_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PGUSER", "u")
    monkeypatch.setenv("PGDATABASE", "d")
    with pytest.raises(SystemExit) as exit_info:
        db.connect()
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == (
        f"Missing database settings: PGHOST, PGPASSWORD. Set them in {db.env_file} "
        "or pass a database URL as the first argument.\n"
    )


def test_connect_with_url_needs_no_pg_settings() -> None:
    db.connect("postgresql://u:p@h:1/d")
    assert db._conninfo == "postgresql://u:p@h:1/d"


def test_connect_without_url_uses_libpq_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE"):
        monkeypatch.setenv(name, "x")
    db.connect()
    assert db._conninfo == ""


def test_connect_drops_empty_pg_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGSSLMODE", "")
    monkeypatch.setenv("PGPORT", "")
    monkeypatch.setenv("PGAPPNAME", "kept")
    db.connect("postgresql://h/d")
    assert "PGSSLMODE" not in os.environ
    assert "PGPORT" not in os.environ
    assert os.environ["PGAPPNAME"] == "kept"


def test_default_connect_timeout() -> None:
    db.connect("postgresql://h/d")
    assert db._connect_options == {"connect_timeout": db.DEFAULT_CONNECT_TIMEOUT_S}


def test_connect_timeout_from_environment_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "3")
    db.connect("postgresql://h/d")
    assert db._connect_options == {}


def test_connect_timeout_from_url_wins() -> None:
    db.connect("postgresql://h/d?connect_timeout=3")
    assert db._connect_options == {}


def test_invalid_url_exits(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        db.connect("postgresql://h/d?no_such_option=1")
    assert exit_info.value.code == 1
    assert capsys.readouterr().err.startswith("Invalid database URL: ")


def test_open_connection_before_connect() -> None:
    with pytest.raises(RuntimeError, match=r"^connect\(\) must be called before"):
        run(db.open_connection())


# ---------------------------------------------------------------- connecting


async def _rows(sql: str) -> list[db.Row]:
    return await db.run_read_only(sql)


def test_connects_with_pg_environment_and_empty_sslmode(
    pg: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PGSSLMODE", "")  # as written by .env.example
    db.connect()
    assert run(_rows("SELECT current_database() AS d")) == [{"d": pg.dbname}]


def test_connects_with_url(pg: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE"):
        monkeypatch.setenv(name, "")
    db.connect(pg.url)
    assert run(_rows("SELECT current_user AS u")) == [{"u": pg.user}]


def test_wrong_password(pg: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGPASSWORD", "wrong")
    db.connect()
    with pytest.raises(psycopg.OperationalError, match="password authentication failed"):
        run(_rows("SELECT 1"))


def test_unreachable_server_times_out(pg: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGHOST", "127.0.0.1")
    monkeypatch.setenv("PGPORT", "1")
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "2")
    db.connect()
    started = time.monotonic()
    with pytest.raises(psycopg.OperationalError):
        run(_rows("SELECT 1"))
    assert time.monotonic() - started < 10


# ---------------------------------------------------------------- run_read_only


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 AS one",
        "SELECT 1 AS one;",
        "  SELECT 1 AS one;; \n ",
        "SELECT 1 AS one -- trailing comment",
        "WITH x AS (SELECT 1 AS one) SELECT * FROM x",
        "\n\tSELECT 1 AS one\n",
    ],
)
def test_runs_selects(connected: Database, sql: str) -> None:
    assert run(_rows(sql)) == [{"one": 1}]


def test_semicolon_before_a_trailing_comment_is_not_stripped(connected: Database) -> None:
    # Known limitation, same as the TypeScript original: only a semicolon at the
    # very end is removed. Postgres rejects this; ask() sends the error to the
    # LLM, which then returns the query without it.
    with pytest.raises(psycopg.errors.SyntaxError):
        run(_rows("SELECT 1 AS one; -- comment after semicolon"))


def test_percent_signs_and_placeholders_are_plain_text(connected: Database) -> None:
    rows = run(_rows("SELECT name FROM customers WHERE name LIKE '%100%' ORDER BY id"))
    assert rows == [{"name": "Ann 100%"}]
    rows = run(_rows("SELECT '%s %(x)s %%' AS t"))
    assert rows == [{"t": "%s %(x)s %%"}]


def test_hebrew_and_quoted_identifiers(connected: Database) -> None:
    rows = run(_rows("SELECT name FROM customers WHERE city = 'חיפה'"))
    assert rows == [{"name": "דנה כהן"}]
    rows = run(_rows('SELECT "Weird Col" FROM sales.orders WHERE id = 1'))
    assert rows == [{"Weird Col": "x"}]


def test_second_statement_breaks_the_subquery(connected: Database) -> None:
    with pytest.raises(psycopg.errors.SyntaxError):
        run(_rows("SELECT 1; DELETE FROM customers"))


def test_escaping_the_subquery_hits_one_statement_rule(connected: Database) -> None:
    escape = "SELECT 1) AS x; COMMIT; DELETE FROM customers; SELECT * FROM (SELECT 1"
    with pytest.raises(psycopg.Error, match="cannot insert multiple commands"):
        run(_rows(escape))


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM customers",
        "UPDATE customers SET name = 'x'",
        "INSERT INTO customers (name) VALUES ('x')",
        "DROP TABLE customers",
        "CREATE TABLE t (a int)",
        "TRUNCATE customers",
        "WITH d AS (DELETE FROM customers RETURNING *) SELECT * FROM d",
        "SELECT nextval('customers_id_seq')",
        "SELECT * FROM customers FOR UPDATE",
        "COMMIT",
        "SET ROLE postgres",
    ],
)
def test_writes_are_refused(connected: Database, sql: str) -> None:
    with pytest.raises(psycopg.Error):
        run(_rows(sql))
    assert run(_rows("SELECT count(*)::int AS n FROM customers")) == [{"n": 3}]


def test_transaction_is_read_only_and_rolled_back(connected: Database) -> None:
    assert run(_rows("SELECT current_setting('transaction_read_only') AS ro")) == [{"ro": "on"}]


def test_statement_timeout(connected: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "query_timeout_ms", 300)
    assert run(_rows("SELECT current_setting('statement_timeout') AS t")) == [{"t": "300ms"}]
    with pytest.raises(psycopg.errors.QueryCanceled, match="statement timeout"):
        run(_rows("SELECT pg_sleep(3)"))


def test_row_cap(connected: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "max_rows", 7)
    assert len(run(_rows('SELECT * FROM "Mixed Case"'))) == 7
    assert len(run(_rows('SELECT * FROM "Mixed Case" LIMIT 3'))) == 3


def test_default_row_cap_from_settings(connected: Database) -> None:
    assert db.max_rows == 1000  # MAX_ROWS set by conftest
    assert len(run(_rows('SELECT * FROM "Mixed Case"'))) == 1000


def test_empty_result(connected: Database) -> None:
    assert run(_rows("SELECT * FROM customers WHERE false")) == []


def test_connections_are_closed(connected: Database) -> None:
    def open_count() -> int:
        with direct_connect(connected.url) as conn:
            row = conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND pid <> pg_backend_pid()"
            ).fetchone()
            assert row is not None
            return int(row[0])

    before = open_count()
    for _ in range(10):
        run(_rows("SELECT 1"))
        with pytest.raises(psycopg.Error):
            run(_rows("SELECT * FROM no_such_table"))
    assert open_count() == before


def test_all_column_types_convert_to_json(connected: Database) -> None:
    orders = json.loads(to_json(run(_rows("SELECT * FROM sales.orders ORDER BY id"))))
    assert orders[0] == {
        "id": 1,
        "customer_id": 1,
        "total": "1234567890.12",
        "ordered_at": "2026-11-01T10:00:00+00:00",
        "ship_date": "2026-11-01",
        "meta": {"a": [1, 2], "h": "שלום"},
        "blob": "\\xdeadbeef",
        "token": "6f1c2b8e-1d2a-4c55-9a8e-0b1f2a3c4d5e",
        "ratio": None,
        "Weird Col": "x",
    }
    assert orders[1]["total"] == "10.50" and orders[1]["ratio"] == 0.25

    [demo] = json.loads(to_json(run(_rows("SELECT * FROM types_demo"))))
    assert demo == {
        "i": "1 day, 2:00:00",
        "ip": "10.0.0.1",
        "r": "[1, 5)",
        "t": "13:45:00",
        "arr": [1, 2, 3],
        "flag": True,
        "big": 9007199254740993,
        "f": None,
    }
