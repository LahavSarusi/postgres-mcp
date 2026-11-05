"""server.py: the MCP handlers in-process, and the server over stdio as an MCP client sees it."""

import json
import sys
from typing import Any

import anyio
import pytest
from conftest import Database, MockLlm, Reply, run_script, script_path
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError

from postgres_mcp import __version__, server
from postgres_mcp.output import run
from postgres_mcp.server import TOOLS, create_server, resource_base

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
    assert resource_base(url) == base


def test_resource_base_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGHOST", "db.internal")
    monkeypatch.setenv("PGPORT", "6000")
    assert resource_base(None) == "postgres://db.internal:6000"
    monkeypatch.setenv("PGPORT", "")
    assert resource_base(None) == "postgres://db.internal:5432"
    monkeypatch.delenv("PGPORT")
    assert resource_base(None) == "postgres://db.internal:5432"


# ---------------------------------------------------------------- handlers in-process


def handler(srv: Any, method: str) -> Any:
    entry = srv.get_request_handler(method)
    assert entry is not None, method
    return entry.handler


def call_tool(srv: Any, name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
    params = types.CallToolRequestParams(name=name, arguments=arguments)
    result: types.CallToolResult = run(handler(srv, "tools/call")(None, params))
    return result


def texts(result: types.CallToolResult) -> list[str]:
    return [c.text for c in result.content if isinstance(c, types.TextContent)]


def test_server_identity_and_capabilities() -> None:
    srv = create_server(None)
    assert srv.name == "postgres-mcp-server"
    assert srv.version == __version__ == "0.1.0"
    capabilities = srv.get_capabilities()
    assert capabilities.tools is not None
    assert capabilities.resources is not None
    assert capabilities.prompts is None


def test_list_tools() -> None:
    result = run(handler(create_server(None), "tools/list")(None, None))
    assert result.tools == TOOLS
    ask_tool, query_tool = TOOLS
    assert ask_tool.name == "ask"
    assert ask_tool.input_schema["required"] == ["question"]
    assert query_tool.name == "query"
    assert query_tool.input_schema["required"] == ["sql"]


def test_query_tool(connected: Database) -> None:
    result = call_tool(
        create_server(None), "query", {"sql": "SELECT name FROM customers ORDER BY id"}
    )
    assert result.is_error is False
    [text] = texts(result)
    assert json.loads(text) == [{"name": "דנה כהן"}, {"name": "Bob"}, {"name": "Ann 100%"}]
    assert text.startswith('[\n  {\n    "name": "דנה כהן"')


def test_ask_tool(connected: Database, llm: MockLlm) -> None:
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
        ("query", {"sql": 5}, "sql must be a non-empty string"),
        ("query", {}, "sql must be a non-empty string"),
        ("query", None, "sql must be a non-empty string"),
        ("ask", {"question": ""}, "question must be a non-empty string"),
        ("ask", {"question": ["x"]}, "question must be a non-empty string"),
        ("ask", None, "question must be a non-empty string"),
    ],
)
def test_invalid_arguments(name: str, arguments: dict[str, Any] | None, message: str) -> None:
    result = call_tool(create_server(None), name, arguments)
    assert result.is_error is True
    assert texts(result) == [message]


def test_query_tool_database_error_is_a_tool_error(connected: Database) -> None:
    result = call_tool(create_server(None), "query", {"sql": "DELETE FROM customers"})
    assert result.is_error is True
    assert texts(result)[0].startswith("syntax error")


def test_ask_tool_llm_error_is_a_tool_error(connected: Database, llm: MockLlm) -> None:
    llm.script(Reply(status=500, raw=b"down"))
    result = call_tool(create_server(None), "ask", {"question": "q"})
    assert result.is_error is True
    assert texts(result) == ["LLM request failed (500): down"]


def test_unknown_tool_is_a_protocol_error() -> None:
    with pytest.raises(MCPError) as error:
        call_tool(create_server(None), "drop_everything", {})
    assert error.value.error.code == types.INVALID_PARAMS
    assert error.value.error.message == "Unknown tool: drop_everything"


def test_list_resources(connected: Database) -> None:
    srv = create_server(None)
    result = run(handler(srv, "resources/list")(None, None))
    base = f"postgres://{connected.host}:{connected.port}"
    by_uri = {r.uri: r for r in result.resources}
    assert sorted(by_uri) == sorted(
        f"{base}/{name}/schema"
        for name in ("Mixed%20Case", "active_customers", "customers", "types_demo")
    )  # public schema only
    mixed = by_uri[f"{base}/Mixed%20Case/schema"]
    assert mixed.name == '"Mixed Case" database schema'
    assert mixed.mime_type == "application/json"


def read_resource(srv: Any, uri: str) -> types.ReadResourceResult:
    params = types.ReadResourceRequestParams(uri=uri)
    result: types.ReadResourceResult = run(handler(srv, "resources/read")(None, params))
    return result


def test_read_resource(connected: Database) -> None:
    uri = "postgres://h:1/Mixed%20Case/schema"
    result = read_resource(create_server(None), uri)
    [content] = result.contents
    assert isinstance(content, types.TextResourceContents)
    assert content.uri == uri
    assert content.mime_type == "application/json"
    assert json.loads(content.text) == [
        {"column_name": "k", "data_type": "integer"},
        {"column_name": "v", "data_type": "text"},
    ]


def test_read_resource_of_unknown_table_is_empty(connected: Database) -> None:
    [content] = read_resource(create_server(None), "postgres://h:1/nothing/schema").contents
    assert isinstance(content, types.TextResourceContents)
    assert content.text == "[]"


@pytest.mark.parametrize(
    "uri", ["postgres://h:1/customers/columns", "postgres://h:1/", "postgres://h:1", "x"]
)
def test_read_resource_rejects_other_uris(uri: str) -> None:
    with pytest.raises(MCPError) as error:
        read_resource(create_server(None), uri)
    assert error.value.error.code == types.INVALID_PARAMS
    assert error.value.error.message == "Invalid resource URI"


def test_resource_errors_keep_their_message(pg: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGPASSWORD", "wrong")
    server.connect()
    srv = create_server(None)
    with pytest.raises(MCPError) as error:
        run(handler(srv, "resources/list")(None, None))
    assert error.value.error.code == types.INTERNAL_ERROR
    assert "password authentication failed" in error.value.error.message
    with pytest.raises(MCPError, match="password authentication failed"):
        read_resource(srv, "postgres://h:1/customers/schema")


# ---------------------------------------------------------------- over stdio


async def session_checks(command: str, args: list[str], expect_base: str) -> None:
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

        with pytest.raises(MCPError, match="Unknown tool: nope"):
            await session.call_tool("nope", {})

        uris = sorted(r.uri for r in (await session.list_resources()).resources)
        assert f"{expect_base}/customers/schema" in uris
        assert all("@" not in uri and "testpw" not in uri for uri in uris)

        read = await session.read_resource(f"{expect_base}/customers/schema")
        read_first = read.contents[0]
        assert isinstance(read_first, types.TextResourceContents)
        assert [c["column_name"] for c in json.loads(read_first.text)] == [
            "id", "name", "city", "status",
        ]  # fmt: skip
        with pytest.raises(MCPError, match="Invalid resource URI"):
            await session.read_resource(f"{expect_base}/customers/other")


def test_stdio_server_with_pg_environment(pg: Database, llm: MockLlm) -> None:
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
    seen: list[Any] = []
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(server, "connect", seen.append)
    monkeypatch.setattr(server, "run", lambda coroutine: coroutine.close())
    server.main()
    assert seen == [None]


def test_main_ctrl_c_is_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
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
    result = run_script([script_path("postgres-mcp-server")], timeout=30)
    assert result.returncode == 1
    assert result.stdout == b""  # stdout belongs to the MCP protocol
    assert result.stderr.decode().startswith("Missing database settings:")


def test_server_exits_on_invalid_url() -> None:
    result = run_script([script_path("postgres-mcp-server"), "postgresql://h/d?bad_option=1"])
    assert result.returncode == 1
    assert result.stderr.decode().startswith("Invalid database URL: ")
