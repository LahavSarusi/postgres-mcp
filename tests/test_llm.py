"""llm.py against the mock OpenAI-compatible server."""

import socket

import pytest
from conftest import MockLlm, Reply

from postgres_mcp import llm as llm_module
from postgres_mcp.llm import (
    SYSTEM_PROMPT,
    LlmError,
    extract_sql,
    generate_sql,
    start_conversation,
)
from postgres_mcp.output import run


@pytest.mark.parametrize(
    ("reply", "sql"),
    [
        ("SELECT 1", "SELECT 1"),
        ("  SELECT 1;\n", "SELECT 1;"),
        ("```sql\nSELECT 1\n```", "SELECT 1"),
        ("```SQL\nSELECT 1\n```", "SELECT 1"),
        ("```\nSELECT 1\n```", "SELECT 1"),
        ("<think>reasoning\nmore</think>\nSELECT 1", "SELECT 1"),
        ("<think>a</think>```sql\nSELECT 1\n```<think>b</think>", "SELECT 1"),
        ("<think>has ```sql SELECT 2``` inside</think>SELECT 1", "SELECT 1"),
        ("<think>only thinking</think>", ""),
    ],
)
def test_extract_sql(reply: str, sql: str) -> None:
    assert extract_sql(reply) == sql


def test_start_conversation() -> None:
    assert start_conversation("כמה לקוחות?", "t(a int)") == [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "Schema:\nt(a int)\n\nQuestion: כמה לקוחות?"},
    ]


def test_system_prompt_rules() -> None:
    assert SYSTEM_PROMPT.startswith("You translate questions about a PostgreSQL database")
    assert "Return only the SQL. No explanation, no markdown fences." in SYSTEM_PROMPT


def test_generate_sql_request_and_reply(llm: MockLlm) -> None:
    llm.script(Reply(content="<think>x</think>```sql\nSELECT 1;\n```"))
    messages = start_conversation("q", "s")
    assert run(generate_sql(messages)) == "SELECT 1;"

    [request] = llm.requests
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["authorization"] == "Bearer test-key"
    assert request["headers"]["content-type"] == "application/json"
    assert request["body"] == {"model": "mock-model", "messages": messages, "temperature": 0}


def test_base_url_trailing_slash(llm: MockLlm, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", llm.base_url + "/")
    llm.script("SELECT 1")
    run(generate_sql(start_conversation("q", "s")))
    assert llm.requests[0]["path"] == "/v1/chat/completions"


def test_follows_redirects(llm: MockLlm) -> None:
    llm.script(
        Reply(status=307, raw=b"", headers={"Location": "/v1/chat/completions"}),
        "SELECT 2",
    )
    assert run(generate_sql(start_conversation("q", "s"))) == "SELECT 2"
    assert len(llm.requests) == 2


@pytest.mark.parametrize("status", [400, 401, 404, 429, 500, 503])
def test_http_error_status(llm: MockLlm, status: int) -> None:
    llm.script(Reply(status=status, raw=b"x" * 900))
    with pytest.raises(LlmError) as error:
        run(generate_sql(start_conversation("q", "s")))
    assert str(error.value) == f"LLM request failed ({status}): {'x' * 500}"


def test_reply_not_json(llm: MockLlm) -> None:
    llm.script(Reply(raw=b"<html>gateway</html>"))
    with pytest.raises(LlmError) as error:
        run(generate_sql(start_conversation("q", "s")))
    assert str(error.value) == "LLM returned a reply that is not JSON: <html>gateway</html>"


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"[]",
        b"null",
        b'{"choices": []}',
        b'{"choices": "x"}',
        b'{"choices": [1]}',
        b'{"choices": [{}]}',
        b'{"choices": [{"message": "x"}]}',
        b'{"choices": [{"message": {}}]}',
        b'{"choices": [{"message": {"content": null}}]}',
        b'{"choices": [{"message": {"content": ""}}]}',
        b'{"choices": [{"message": {"content": ["SELECT 1"]}}]}',
    ],
)
def test_reply_without_content(llm: MockLlm, raw: bytes) -> None:
    llm.script(Reply(raw=raw))
    with pytest.raises(LlmError, match=r"^LLM returned no content$"):
        run(generate_sql(start_conversation("q", "s")))


@pytest.mark.parametrize("content", ["<think>hmm</think>", "```sql\n```", "   "])
def test_reply_without_sql(llm: MockLlm, content: str) -> None:
    llm.script(Reply(content=content))
    with pytest.raises(LlmError, match=r"^LLM returned no SQL$"):
        run(generate_sql(start_conversation("q", "s")))


def test_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}/v1"
    monkeypatch.setenv("LLM_BASE_URL", url)
    with pytest.raises(LlmError) as error:
        run(generate_sql(start_conversation("q", "s")))
    message = str(error.value)
    assert message.startswith(f"Could not reach the LLM at {url}/chat/completions: ")
    assert len(message) > len(f"Could not reach the LLM at {url}/chat/completions: ")


def test_timeout(llm: MockLlm, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_module, "REQUEST_TIMEOUT_S", 0.5)
    llm.script(Reply(content="SELECT 1", delay=3))
    with pytest.raises(LlmError) as error:
        run(generate_sql(start_conversation("q", "s")))
    assert str(error.value) == (
        f"Could not reach the LLM at {llm.base_url}/chat/completions: no reply within 0.5 s"
    )


def test_missing_settings_fail_before_any_request(
    llm: MockLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "")
    with pytest.raises(RuntimeError, match=r"^Missing LLM settings: LLM_API_KEY"):
        run(generate_sql(start_conversation("q", "s")))
    assert llm.requests == []


def test_llm_error_is_a_runtime_error() -> None:
    assert issubclass(LlmError, RuntimeError)
