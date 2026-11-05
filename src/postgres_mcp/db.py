"""The database connection and the one function that runs SQL on behalf of users and the LLM.

SQL reaching run_read_only may come from a model with no human reading it
first, so it is confined by four independent layers:
  1. one statement only: the query is sent with a bound parameter, which makes
     psycopg use the extended protocol, and Postgres refuses more than one
     command there (this is what stops "COMMIT; DELETE ..." escaping the
     transaction);
  2. a READ ONLY transaction, always rolled back;
  3. the SQL is wrapped as a subquery, so only a row-returning query parses;
  4. a statement_timeout and a row cap, so one query cannot hang or flood.
A database user with only SELECT rights remains the recommended outer layer.

Each operation opens its own connection and closes it afterwards. The cost is
small next to an LLM call, and a failed connection reports its real cause (a
wrong password, a refused port) at once instead of after a pool's wait.
"""

import contextlib
import os
import re
import sys
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from postgres_mcp.config import env_file, max_rows, query_timeout_ms

Row = dict[str, Any]
Connection = psycopg.AsyncConnection[Row]

_conninfo: str | None = None
_connect_options: dict[str, Any] = {}

# On Windows a refused connection is never reported to libpq's wait, so without
# a limit connecting to a stopped server hangs forever.
DEFAULT_CONNECT_TIMEOUT_S = 10

_TRAILING_SEMICOLONS = re.compile(r";+\s*\Z")


def connect(database_url: str | None = None) -> None:
    """Sets where to connect.

    With a URL it connects there; without one, libpq reads PGHOST, PGPORT,
    PGUSER, PGPASSWORD, PGDATABASE and PGSSLMODE itself.
    """
    global _conninfo
    if not database_url:
        missing = [
            name
            for name in ("PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE")
            if not os.environ.get(name)
        ]
        if missing:
            print(
                f"Missing database settings: {', '.join(missing)}. "
                f"Set them in {env_file} or pass a database URL as the first argument.",
                file=sys.stderr,
            )
            sys.exit(1)
    # libpq takes an empty variable (such as "PGSSLMODE=" from .env) as a value,
    # and rejects an empty sslmode; removing empty ones makes them mean "not set".
    for name in [n for n, v in os.environ.items() if n.startswith("PG") and v == ""]:
        del os.environ[name]
    _conninfo = database_url or ""
    try:
        settings = conninfo_to_dict(_conninfo)
    except psycopg.Error as error:
        print(f"Invalid database URL: {error}", file=sys.stderr)
        sys.exit(1)
    # A timeout set in the URL or in PGCONNECT_TIMEOUT wins over the default.
    if "connect_timeout" not in settings and not os.environ.get("PGCONNECT_TIMEOUT"):
        _connect_options["connect_timeout"] = DEFAULT_CONNECT_TIMEOUT_S


async def open_connection() -> Connection:
    """Opens a new autocommit connection whose rows come back as dicts."""
    if _conninfo is None:
        raise RuntimeError("connect() must be called before using the database")
    return await psycopg.AsyncConnection.connect(
        _conninfo, autocommit=True, row_factory=dict_row, **_connect_options
    )


async def run_read_only(sql: str) -> list[Row]:
    """Runs one read-only query and returns at most MAX_ROWS rows."""
    # A trailing semicolon would end the subquery early; the newlines keep a
    # trailing "-- comment" from swallowing the closing parenthesis. "%" is
    # doubled because psycopg reads "%s" placeholders in the query text.
    body = _TRAILING_SEMICOLONS.sub("", sql.strip()).replace("%", "%%")
    wrapped = f"SELECT * FROM (\n{body}\n) AS q LIMIT %s"

    conn = await open_connection()
    try:
        await conn.execute("BEGIN TRANSACTION READ ONLY")
        # SET cannot take a bound parameter; query_timeout_ms is a validated integer.
        await conn.execute(f"SET LOCAL statement_timeout = {query_timeout_ms}")
        cursor = await conn.execute(wrapped, (max_rows,))
        return await cursor.fetchall()
    finally:
        # A connection that cannot roll back is closed below either way.
        with contextlib.suppress(psycopg.Error):
            await conn.execute("ROLLBACK")
        await conn.close()
