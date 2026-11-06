"""MCP server entry point.

It exposes the database to MCP clients through two tools: `ask` (free text in,
rows out, with the configured LLM writing the SQL) and `query` (SQL in, rows
out), plus one schema resource per table. Settings come from .env (see
config.py); a database URL given as the first argument overrides the PG*
settings. All SQL from users and the LLM runs through db.run_read_only.
"""

import os
import sys
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar
from urllib.parse import quote, unquote, urlsplit

from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from postgres_mcp import __version__
from postgres_mcp.ask import ask
from postgres_mcp.db import connect, open_connection, run_read_only
from postgres_mcp.output import error_text, run, to_json

SCHEMA_PATH = "schema"

# The MCP spec's error code for a resource that does not exist; the SDK has no constant for it.
RESOURCE_NOT_FOUND = -32002

Context = ServerRequestContext[Any]
P = ParamSpec("P")
R = TypeVar("R")


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


def _report_errors(handler: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Sends a failing handler's message to the client.

    Without this, the SDK replaces it with a generic "Internal server error".
    """

    @wraps(handler)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await handler(*args, **kwargs)
        except MCPError:
            raise
        except Exception as error:
            raise MCPError(code=types.INTERNAL_ERROR, message=error_text(error)) from error

    return wrapper


TOOLS = [
    types.Tool(
        name="ask",
        description=(
            "Answer a question about the database in free text (any language). "
            "An LLM writes the SQL, which runs read-only; returns the matching rows "
            "as a JSON array, followed by the SQL that produced them."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The data you want, in plain language",
                },
            },
            "required": ["question"],
        },
    ),
    types.Tool(
        name="query",
        description="Run a single read-only SQL query; returns the rows as a JSON array",
        input_schema={
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "One PostgreSQL SELECT query"},
            },
            "required": ["sql"],
        },
    ),
]


def _text_result(*texts: str, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text) for text in texts],
        is_error=is_error,
    )


def create_server(database_url: str | None) -> Server[Any]:
    base = resource_base(database_url)

    @_report_errors
    async def list_resources(
        ctx: Context, params: types.PaginatedRequestParams | None
    ) -> types.ListResourcesResult:
        async with await open_connection() as conn:
            cursor = await conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
            rows = await cursor.fetchall()
        return types.ListResourcesResult(
            resources=[
                types.Resource(
                    uri=f"{base}/{quote(row['table_name'], safe='')}/{SCHEMA_PATH}",
                    mime_type="application/json",
                    name=f'"{row["table_name"]}" database schema',
                )
                for row in rows
            ]
        )

    @_report_errors
    async def read_resource(
        ctx: Context, params: types.ReadResourceRequestParams
    ) -> types.ReadResourceResult:
        path_components = urlsplit(params.uri).path.split("/")
        schema = path_components.pop() if path_components else None
        table_name = unquote(path_components.pop()) if path_components else None

        if schema != SCHEMA_PATH:
            raise MCPError(code=types.INVALID_PARAMS, message="Invalid resource URI")

        async with await open_connection() as conn:
            cursor = await conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s "
                "ORDER BY ordinal_position",
                (table_name,),
            )
            rows = await cursor.fetchall()
            # No columns usually means no such table (a typo, or a dropped table);
            # a table that exists with zero columns still returns [].
            if not rows:
                cursor = await conn.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = %s",
                    (table_name,),
                )
                if await cursor.fetchone() is None:
                    raise MCPError(
                        code=RESOURCE_NOT_FOUND, message=f'Table "{table_name}" not found'
                    )
        return types.ReadResourceResult(
            contents=[
                types.TextResourceContents(
                    uri=params.uri, mime_type="application/json", text=to_json(rows)
                )
            ]
        )

    async def list_tools(
        ctx: Context, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=TOOLS)

    async def call_tool(ctx: Context, params: types.CallToolRequestParams) -> types.CallToolResult:
        args = params.arguments or {}
        try:
            if params.name == "ask":
                question = args.get("question")
                if not isinstance(question, str) or not question.strip():
                    raise ValueError("question must be a non-empty string")
                result = await ask(question)
                return _text_result(to_json(result.rows), f"SQL:\n{result.sql}")
            if params.name == "query":
                sql = args.get("sql")
                if not isinstance(sql, str) or not sql.strip():
                    raise ValueError("sql must be a non-empty string")
                return _text_result(to_json(await run_read_only(sql)))
        except Exception as error:
            return _text_result(error_text(error), is_error=True)
        raise MCPError(code=types.INVALID_PARAMS, message=f"Unknown tool: {params.name}")

    return Server(
        "postgres-mcp-server",
        version=__version__,
        on_list_resources=list_resources,
        on_read_resource=read_resource,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def serve(server: Server[Any]) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    database_url = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
    connect(database_url)
    server = create_server(database_url)
    try:
        run(serve(server))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(error_text(error), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
