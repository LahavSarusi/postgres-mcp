"""MCP server entry point.

It exposes the database to MCP clients through two tools: `ask` (free text in,
rows out, with the configured LLM writing the SQL) and `query` (SQL in, rows
out), plus one schema resource per table. Settings come from .env (see
config.py); a database URL given as the first argument overrides the PG*
settings. All SQL from users and the LLM runs through db.run_read_only.

Built on the SDK's MCPServer (FastMCP before mcp 2): tools and the resource
template are plain decorated functions.
"""

import contextlib
import os
import sys
from typing import Annotated, Any
from urllib.parse import quote, urlsplit

from mcp import types
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.resources.templates import ResourceSecurity
from mcp.shared.exceptions import MCPError
from pydantic import Field

from postgres_mcp import __version__
from postgres_mcp.ask import ask as ask_question
from postgres_mcp.db import connect, open_connection, run_read_only
from postgres_mcp.output import error_text, exit_on_error, run, to_json

SCHEMA_PATH = "schema"

# The MCP spec's error code for a resource that does not exist; the SDK has no constant for it.
RESOURCE_NOT_FOUND = -32002

INSTRUCTIONS = (
    "Read-only access to a PostgreSQL database. Use `query` when you can write the SQL "
    "yourself; read the table schema resources first to see the columns. Use `ask` to "
    "have the server's own LLM write the SQL from a plain-language question. Both return "
    "rows as a JSON array, capped at the server's row limit."
)


def resource_base(database_url: str | None) -> str:
    """Base for the table-schema resource URIs: "postgres://host:port". No credentials."""
    if database_url:
        parts = urlsplit(database_url)
        host = parts.hostname or ""
        port: int | str | None = parts.port
    else:
        host = os.environ.get("PGHOST", "")
        port = os.environ.get("PGPORT") or "5432"
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    return f"postgres://{host}" + (f":{port}" if port else "")


def _text_result(*texts: str, is_error: bool = False) -> types.CallToolResult:
    """A tool result with one text block per argument."""
    return types.CallToolResult(
        content=[types.TextContent(text=text) for text in texts], is_error=is_error
    )


def _internal_error(error: Exception) -> MCPError:
    """An MCP error carrying the failure's own message.

    Any other exception from a resource handler reaches the client only as a
    generic "Internal server error" or "Error reading resource".
    """
    return MCPError(code=types.INTERNAL_ERROR, message=error_text(error))


class PostgresServer(MCPServer[Any]):
    """MCPServer that lists one schema resource per table in the public schema.

    The resource is a template, which MCPServer would only list under
    resources/templates/list; listing every table lets clients show them.
    """

    def __init__(self, base: str) -> None:
        super().__init__("postgres-mcp-server", version=__version__, instructions=INSTRUCTIONS)
        self.base = base

    async def list_resources(self) -> list[types.Resource]:
        """One schema resource per table and view in the public schema, sorted by name."""
        try:
            async with await open_connection() as conn:
                cursor = await conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    # "C": the same order whatever the database's collation.
                    "WHERE table_schema = 'public' ORDER BY table_name COLLATE \"C\""
                )
                rows = await cursor.fetchall()
        except Exception as error:
            raise _internal_error(error) from error
        return [
            types.Resource(
                uri=f"{self.base}/{quote(row['table_name'], safe='')}/{SCHEMA_PATH}",
                mime_type="application/json",
                name=f'"{row["table_name"]}" database schema',
            )
            for row in rows
        ]


def create_server(database_url: str | None) -> PostgresServer:
    """The server with the `ask` and `query` tools and the table schema resource."""
    server = PostgresServer(resource_base(database_url))

    @server.tool(
        title="Ask the database",
        description=(
            "Answer a question about the database in free text (any language). "
            "An LLM writes the SQL, which runs read-only; returns the matching rows "
            "as a JSON array, followed by the SQL that produced them."
        ),
        # Open world: the question goes to an external LLM, whose SQL may differ per call.
        annotations=types.ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def ask(
        question: Annotated[str, Field(description="The data you want, in plain language")],
    ) -> types.CallToolResult:
        if not question.strip():
            return _text_result("question must be a non-empty string", is_error=True)
        try:
            result = await ask_question(question)
        except Exception as error:
            return _text_result(error_text(error), is_error=True)
        return _text_result(to_json(result.rows), f"SQL:\n{result.sql}")

    @server.tool(
        title="Run a SQL query",
        description="Run a single read-only SQL query; returns the rows as a JSON array",
        annotations=types.ToolAnnotations(
            read_only_hint=True, idempotent_hint=True, open_world_hint=False
        ),
    )
    async def query(
        sql: Annotated[str, Field(description="One PostgreSQL SELECT query")],
    ) -> types.CallToolResult:
        if not sql.strip():
            return _text_result("sql must be a non-empty string", is_error=True)
        try:
            rows = await run_read_only(sql)
        except Exception as error:
            return _text_result(error_text(error), is_error=True)
        return _text_result(to_json(rows))

    # The table name only ever reaches SQL as a bound parameter, so the
    # template's file-path checks (.., absolute paths) do not apply to it.
    @server.resource(
        f"{server.base}/{{table}}/{SCHEMA_PATH}",
        name="table schema",
        description="Columns and data types of a table in the public schema",
        mime_type="application/json",
        security=ResourceSecurity(exempt_params={"table"}),
    )
    async def table_schema(table: str) -> str:
        """The table's columns and their data types, in table order, as JSON."""
        try:
            async with await open_connection() as conn:
                cursor = await conn.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = %s "
                    "ORDER BY ordinal_position",
                    (table,),
                )
                columns = await cursor.fetchall()
                # No columns usually means no such table (a typo, or a dropped table),
                # but a table can exist with zero columns.
                exists = bool(columns)
                if not exists:
                    cursor = await conn.execute(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = %s",
                        (table,),
                    )
                    exists = await cursor.fetchone() is not None
        except Exception as error:
            raise _internal_error(error) from error
        if not exists:
            raise MCPError(code=RESOURCE_NOT_FOUND, message=f'Table "{table}" not found')
        return to_json(columns)

    return server


def main() -> None:
    """Entry point of postgres-mcp-server: serves MCP over stdio until the client disconnects."""
    database_url = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
    with exit_on_error():
        connect(database_url)
        server = create_server(database_url)
        # output.run, not server.run(): that uses anyio.run, whose default loop on
        # Windows (Proactor) does not work with psycopg's async mode.
        with contextlib.suppress(KeyboardInterrupt):  # Ctrl+C stops the server quietly
            run(server.run_stdio_async())


if __name__ == "__main__":
    main()
