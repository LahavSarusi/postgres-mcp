"""server.py: the MCP server via an in-process client, and over stdio as a real client sees it."""

import json
import sys
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from urllib.parse import quote

import anyio
import pytest
from conftest import Database, MockLlm, Reply, direct_connect, run_script, script_path
from mcp import Client, types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError

from postgres_mcp import __version__, server
from postgres_mcp.output import run
from postgres_mcp.server import (
    INSTRUCTIONS,
    RESOURCE_NOT_FOUND,
    PostgresServer,
    create_server,
    resource_base,
)

# ---------------------------------------------------------------- resource_base


@pytest.mark.parametrize(
    ("url", "base"),
    [
        ("postgresql://user:secret@db.example.com:6543/app", "postgres://db.example.com:6543"),
        ("postgres://user:secret@db.example.com/app?sslmode=require", "postgres://db.example.com"),
        ("postgresql://u:p@[::1]:5432/app", "postgres://[::1]:5432"),
        ("postgresql:///app", "postgres://"),
    ],
)
def test_resource_base_from_url_has_no_credentials(url: str, base: str) -> None:
    """The resource URI base keeps the URL's host and port, never its user, password, database or
    options.
    """
    assert resource_base(url) == base


def test_resource_base_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a URL the base comes from PGHOST and PGPORT, with port 5432 when PGPORT is empty or
    unset.
    """
    monkeypatch.setenv("PGHOST", "db.internal")
    monkeypatch.setenv("PGPORT", "6000")
    assert resource_base(None) == "postgres://db.internal:6000"
    monkeypatch.setenv("PGPORT", "")
    assert resource_base(None) == "postgres://db.internal:5432"
    monkeypatch.delenv("PGPORT")
    assert resource_base(None) == "postgres://db.internal:5432"


# ---------------------------------------------------------------- in-process client

T = TypeVar("T")


def with_client(srv: PostgresServer, action: Callable[[Client], Awaitable[T]]) -> T:
    """Connects an in-process MCP client to `srv`, runs `action` with it and returns its result."""

    async def go() -> T:
        # Raised inside the client's task group, an MCPError would come out wrapped
        # in an ExceptionGroup; re-raising it after the client closes keeps it bare.
        failure: MCPError | None = None
        async with Client(srv) as client:
            try:
                return await action(client)
            except MCPError as error:
                failure = error
        raise failure

    return run(go())


def call_tool(
    srv: PostgresServer, name: str, arguments: dict[str, Any] | None
) -> types.CallToolResult:
    """Calls a tool through an in-process client."""
    return with_client(srv, lambda client: client.call_tool(name, arguments))


def read_resource(srv: PostgresServer, uri: str) -> types.ReadResourceResult:
    """Reads a resource through an in-process client."""
    return with_client(srv, lambda client: client.read_resource(uri))


def texts(result: types.CallToolResult) -> list[str]:
    """The text parts of a tool result."""
    return [c.text for c in result.content if isinstance(c, types.TextContent)]


def test_server_identity_and_capabilities() -> None:
    """The server reports its name, version and usage instructions, and offers tools and
    resources.
    """

    async def check(client: Client) -> None:
        assert client.server_info is not None
        assert client.server_info.name == "postgres-mcp-server"
        assert client.server_info.version == __version__ == "0.1.0"
        assert client.instructions == INSTRUCTIONS
        assert "`query`" in INSTRUCTIONS and "`ask`" in INSTRUCTIONS
        assert client.server_capabilities.tools is not None
        assert client.server_capabilities.resources is not None
        assert (await client.list_prompts()).prompts == []

    with_client(create_server(None), check)


def test_list_tools() -> None:
    """tools/list returns ask, which needs a question, and query, which needs sql."""
    tools = with_client(create_server(None), lambda client: client.list_tools()).tools
    ask_tool, query_tool = sorted(tools, key=lambda tool: tool.name)
    assert ask_tool.name == "ask"
    assert ask_tool.description is not None
    assert ask_tool.description.startswith("Answer a question about the database in free text")
    assert ask_tool.input_schema["required"] == ["question"]
    assert ask_tool.input_schema["properties"]["question"]["type"] == "string"
    assert query_tool.name == "query"
    assert query_tool.description == (
        "Run a single read-only SQL query; returns the rows as a JSON array"
    )
    assert query_tool.input_schema["required"] == ["sql"]
    assert query_tool.input_schema["properties"]["sql"]["type"] == "string"


def test_tool_annotations() -> None:
    """Both tools are marked read-only; query is idempotent and closed-world, while ask calls
    an external LLM.
    """
    tools = with_client(create_server(None), lambda client: client.list_tools()).tools
    by_name = {tool.name: tool for tool in tools}
    assert by_name["ask"].title == "Ask the database"
    assert by_name["ask"].annotations == types.ToolAnnotations(
        read_only_hint=True, open_world_hint=True
    )
    assert by_name["query"].title == "Run a SQL query"
    assert by_name["query"].annotations == types.ToolAnnotations(
        read_only_hint=True, idempotent_hint=True, open_world_hint=False
    )


def test_query_tool(connected: Database) -> None:
    """The query tool returns the rows as indented JSON text."""
    result = call_tool(
        create_server(None), "query", {"sql": "SELECT name FROM customers ORDER BY id"}
    )
    assert result.is_error is False
    [text] = texts(result)
    assert json.loads(text) == [{"name": "דנה כהן"}, {"name": "Bob"}, {"name": "Ann 100%"}]
    assert text.startswith('[\n  {\n    "name": "דנה כהן"')


def test_ask_tool(connected: Database, llm: MockLlm) -> None:
    """The ask tool returns the rows, then the SQL it ran."""
    llm.script("SELECT count(*)::int AS n FROM customers")
    result = call_tool(create_server(None), "ask", {"question": "how many customers?"})
    assert result.is_error is False
    assert texts(result) == [
        '[\n  {\n    "n": 3\n  }\n]',
        "SQL:\nSELECT count(*)::int AS n FROM customers",
    ]


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        ("query", {"sql": ""}, "sql must be a non-empty string"),
        ("query", {"sql": "  \n"}, "sql must be a non-empty string"),
        ("ask", {"question": ""}, "question must be a non-empty string"),
    ],
)
def test_blank_arguments(name: str, arguments: dict[str, Any], message: str) -> None:
    """Blank strings give a tool error that names the argument."""
    result = call_tool(create_server(None), name, arguments)
    assert result.is_error is True
    assert texts(result) == [message]


@pytest.mark.parametrize(
    ("name", "arguments", "field"),
    [
        ("query", {"sql": 5}, "sql"),
        ("query", {}, "sql"),
        ("query", None, "sql"),
        ("ask", {"question": ["x"]}, "question"),
        ("ask", None, "question"),
    ],
)
def test_missing_or_non_string_arguments(
    name: str, arguments: dict[str, Any] | None, field: str
) -> None:
    """Missing or non-string arguments are rejected by the input schema as a tool error."""
    result = call_tool(create_server(None), name, arguments)
    assert result.is_error is True
    [text] = texts(result)
    assert text.startswith(f"Error executing tool {name}: 1 validation error")
    assert f"\n{field}\n" in text


def test_query_tool_database_error_is_a_tool_error(connected: Database) -> None:
    """A refused query is a tool error with PostgreSQL's message."""
    result = call_tool(create_server(None), "query", {"sql": "DELETE FROM customers"})
    assert result.is_error is True
    assert texts(result)[0].startswith("syntax error")


def test_ask_tool_llm_error_is_a_tool_error(connected: Database, llm: MockLlm) -> None:
    """An LLM failure is a tool error with its message."""
    llm.script(Reply(status=500, raw=b"down"))
    result = call_tool(create_server(None), "ask", {"question": "q"})
    assert result.is_error is True
    assert texts(result) == ["LLM request failed (500): down"]


def test_unknown_tool_is_a_tool_error() -> None:
    """An unknown tool name is a tool error naming it."""
    result = call_tool(create_server(None), "drop_everything", {})
    assert result.is_error is True
    assert texts(result) == ["Unknown tool: drop_everything"]


def test_list_resources(connected: Database) -> None:
    """resources/list has one JSON schema resource per table and view in the public schema, in
    byte order of the name, the name URL-encoded in the URI.
    """
    result = with_client(create_server(None), lambda client: client.list_resources())
    base = f"postgres://{connected.host}:{connected.port}"
    by_uri = {r.uri: r for r in result.resources}
    assert list(by_uri) == [
        f"{base}/{name}/schema"
        for name in ("Mixed%20Case", "active_customers", "customers", "types_demo")
    ]  # public schema only
    mixed = by_uri[f"{base}/Mixed%20Case/schema"]
    assert mixed.name == '"Mixed Case" database schema'
    assert mixed.mime_type == "application/json"


def test_list_resource_templates() -> None:
    """The schema resource is also offered as one template, {table} in place of the name."""
    srv = create_server("postgresql://u:secret@db.example.com:6543/app")
    result = with_client(srv, lambda client: client.list_resource_templates())
    [template] = result.resource_templates
    assert template.uri_template == "postgres://db.example.com:6543/{table}/schema"
    assert template.mime_type == "application/json"


def test_read_resource(connected: Database) -> None:
    """Reading a table's schema resource returns its column names and types as JSON."""
    srv = create_server(None)
    uri = f"{srv.base}/Mixed%20Case/schema"
    [content] = read_resource(srv, uri).contents
    assert isinstance(content, types.TextResourceContents)
    assert content.uri == uri
    assert content.mime_type == "application/json"
    assert json.loads(content.text) == [
        {"column_name": "k", "data_type": "integer"},
        {"column_name": "v", "data_type": "text"},
    ]


def test_read_resource_of_unknown_table_is_not_found(connected: Database) -> None:
    """An unknown table gives the resource-not-found error, -32002."""
    srv = create_server(None)
    with pytest.raises(MCPError) as error:
        read_resource(srv, f"{srv.base}/custmers/schema")
    assert error.value.error.code == RESOURCE_NOT_FOUND == -32002
    assert error.value.error.message == 'Table "custmers" not found'


def test_read_resource_of_table_outside_public_is_not_found(connected: Database) -> None:
    """A table outside the public schema is not found."""
    # sales.orders exists, but resources cover the public schema only.
    srv = create_server(None)
    with pytest.raises(MCPError) as error:
        read_resource(srv, f"{srv.base}/orders/schema")
    assert error.value.error.code == RESOURCE_NOT_FOUND
    assert error.value.error.message == 'Table "orders" not found'


def test_read_resource_ignores_same_name_in_other_schemas(connected: Database) -> None:
    """A public table is read without the columns of a same-named table in another schema."""
    # information_schema.routines is a built-in view with the same name.
    with direct_connect(connected.url, autocommit=True) as conn:
        conn.execute("CREATE TABLE routines (k int, v text)")
    try:
        srv = create_server(None)
        [content] = read_resource(srv, f"{srv.base}/routines/schema").contents
        assert isinstance(content, types.TextResourceContents)
        assert json.loads(content.text) == [
            {"column_name": "k", "data_type": "integer"},
            {"column_name": "v", "data_type": "text"},
        ]
    finally:
        with direct_connect(connected.url, autocommit=True) as conn:
            conn.execute("DROP TABLE routines")


def test_read_resource_of_table_without_columns_is_empty(connected: Database) -> None:
    """A table with no columns gives an empty list, not an error."""
    with direct_connect(connected.url, autocommit=True) as conn:
        conn.execute("CREATE TABLE no_columns ()")
    try:
        srv = create_server(None)
        [content] = read_resource(srv, f"{srv.base}/no_columns/schema").contents
        assert isinstance(content, types.TextResourceContents)
        assert content.text == "[]"
    finally:
        with direct_connect(connected.url, autocommit=True) as conn:
            conn.execute("DROP TABLE no_columns")


def test_read_resource_of_unusual_table_names(connected: Database) -> None:
    """Names with a slash or dots are plain table names, not paths."""
    with direct_connect(connected.url, autocommit=True) as conn:
        conn.execute('CREATE TABLE "a/b" (k int)')
        conn.execute('CREATE TABLE ".." (k int)')
    try:
        srv = create_server(None)
        for name in ("a/b", ".."):
            uri = f"{srv.base}/{quote(name, safe='')}/schema"
            [content] = read_resource(srv, uri).contents
            assert isinstance(content, types.TextResourceContents)
            assert json.loads(content.text) == [{"column_name": "k", "data_type": "integer"}]
    finally:
        with direct_connect(connected.url, autocommit=True) as conn:
            conn.execute('DROP TABLE "a/b", ".."')


@pytest.mark.parametrize("path", ["/customers/columns", "/", "", "/customers", "/a/b/schema"])
def test_read_resource_rejects_other_uris(path: str) -> None:
    """URIs not of the form <base>/<table>/schema are INVALID_PARAMS errors."""
    srv = create_server(None)
    for uri in (f"{srv.base}{path}", f"postgres://other.host:1{path}", "x"):
        with pytest.raises(MCPError) as error:
            read_resource(srv, uri)
        assert error.value.error.code == types.INVALID_PARAMS
        assert error.value.error.message == f"Unknown resource: {uri}"


def test_resource_errors_keep_their_message(pg: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Database errors while listing or reading resources keep PostgreSQL's message."""
    monkeypatch.setenv("PGPASSWORD", "wrong")
    server.connect()
    srv = create_server(None)
    with pytest.raises(MCPError) as error:
        with_client(srv, lambda client: client.list_resources())
    assert error.value.error.code == types.INTERNAL_ERROR
    assert "password authentication failed" in error.value.error.message
    with pytest.raises(MCPError) as error:
        read_resource(srv, f"{srv.base}/customers/schema")
    assert error.value.error.code == types.INTERNAL_ERROR
    assert "password authentication failed" in error.value.error.message


# ---------------------------------------------------------------- over stdio


async def session_checks(command: str, args: list[str], expect_base: str) -> None:
    """Starts the server as `command args` and checks it as an MCP client.

    Covers identity, tools, tool and protocol errors, and resources, whose URIs must start with
    `expect_base` and hold no credentials.
    """
    import os

    params = StdioServerParameters(command=command, args=args, env=dict(os.environ))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert init.server_info.name == "postgres-mcp-server"
        assert init.server_info.version == __version__

        assert [t.name for t in (await session.list_tools()).tools] == ["ask", "query"]

        result = await session.call_tool("query", {"sql": "SELECT name FROM customers ORDER BY id"})
        assert not result.is_error
        first = result.content[0]
        assert isinstance(first, types.TextContent)
        assert json.loads(first.text)[0] == {"name": "דנה כהן"}

        result = await session.call_tool("ask", {"question": "count"})
        assert not result.is_error and len(result.content) == 2

        result = await session.call_tool("query", {"sql": "DROP TABLE customers"})
        assert result.is_error

        result = await session.call_tool("nope", {})
        assert result.is_error
        assert [c.text for c in result.content if isinstance(c, types.TextContent)] == [
            "Unknown tool: nope"
        ]

        uris = sorted(r.uri for r in (await session.list_resources()).resources)
        assert f"{expect_base}/customers/schema" in uris
        assert all("@" not in uri and "testpw" not in uri for uri in uris)

        read = await session.read_resource(f"{expect_base}/customers/schema")
        read_first = read.contents[0]
        assert isinstance(read_first, types.TextResourceContents)
        assert [c["column_name"] for c in json.loads(read_first.text)] == [
            "id", "name", "city", "status",
        ]  # fmt: skip
        with pytest.raises(MCPError, match="Unknown resource"):
            await session.read_resource(f"{expect_base}/customers/other")
        with pytest.raises(MCPError, match='Table "custmers" not found') as not_found:
            await session.read_resource(f"{expect_base}/custmers/schema")
        assert not_found.value.error.code == RESOURCE_NOT_FOUND


def test_stdio_server_with_pg_environment(pg: Database, llm: MockLlm) -> None:
    """The installed postgres-mcp-server, set up by PG* variables, passes the client checks."""
    llm.script("SELECT 1 AS one")
    anyio.run(
        session_checks,
        script_path("postgres-mcp-server"),
        [],
        f"postgres://{pg.host}:{pg.port}",
    )


def test_stdio_server_with_url_argument_via_python_m(
    pg: Database, llm: MockLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`python -m postgres_mcp <url>`, with no PG* variables, passes the client checks."""
    for name in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE"):
        monkeypatch.setenv(name, "")
    llm.script("SELECT 1 AS one")
    anyio.run(
        session_checks,
        sys.executable,
        ["-m", "postgres_mcp", pg.url],
        f"postgres://{pg.host}:{pg.port}",
    )


def test_main_passes_the_url_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() connects with the URL given as the first argument, then runs the server."""
    seen: dict[str, Any] = {}

    def fake_run(coroutine: Any) -> None:
        coroutine.close()
        seen["ran"] = True

    monkeypatch.setattr(sys, "argv", ["postgres-mcp-server", "postgresql://u:p@h:7/d"])
    monkeypatch.setattr(server, "connect", lambda url: seen.setdefault("url", url))
    monkeypatch.setattr(server, "run", fake_run)
    server.main()
    assert seen == {"url": "postgresql://u:p@h:7/d", "ran": True}


@pytest.mark.parametrize("argv", [["postgres-mcp-server"], ["postgres-mcp-server", ""]])
def test_main_without_url_uses_pg_settings(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    """With no argument, or an empty one, main() connects with the PG* settings."""
    seen: list[Any] = []
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(server, "connect", seen.append)
    monkeypatch.setattr(server, "run", lambda coroutine: coroutine.close())
    server.main()
    assert seen == [None]


def test_main_ctrl_c_is_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl+C stops the server quietly, without an error exit."""

    def interrupted(coroutine: Any) -> None:
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(sys, "argv", ["postgres-mcp-server"])
    monkeypatch.setattr(server, "connect", lambda url: None)
    monkeypatch.setattr(server, "run", interrupted)
    server.main()  # returns normally


def test_main_crash_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Any other error while serving is printed to stderr and exits with code 1."""

    def crashed(coroutine: Any) -> None:
        coroutine.close()
        raise OSError("stdin closed")

    monkeypatch.setattr(sys, "argv", ["postgres-mcp-server"])
    monkeypatch.setattr(server, "connect", lambda url: None)
    monkeypatch.setattr(server, "run", crashed)
    with pytest.raises(SystemExit) as exit_info:
        server.main()
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "stdin closed\n"


def test_server_exits_without_database_settings() -> None:
    """Without database settings the server exits with code 1, writing only to stderr."""
    result = run_script([script_path("postgres-mcp-server")], timeout=30)
    assert result.returncode == 1
    assert result.stdout == b""  # stdout belongs to the MCP protocol
    assert result.stderr.decode().startswith("Missing database settings:")


def test_server_exits_on_invalid_url() -> None:
    """An invalid URL argument exits with code 1."""
    result = run_script([script_path("postgres-mcp-server"), "postgresql://h/d?bad_option=1"])
    assert result.returncode == 1
    assert result.stderr.decode().startswith("Invalid database URL: ")
