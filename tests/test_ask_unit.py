"""ask.py with the schema, LLM and database replaced by stubs."""

from typing import Any

import psycopg
import pytest

from postgres_mcp import ask as ask_module
from postgres_mcp.ask import AskResult, ask
from postgres_mcp.llm import ChatMessage, LlmError
from postgres_mcp.output import run


class Stubs:
    """Stand-ins for describe_schema, generate_sql and run_read_only.

    They hand out the queued replies and results in order, raising any that are exceptions, and
    record what they were called with.
    """

    def __init__(self, sql_replies: list[str | Exception], run_results: list[Any]) -> None:
        self.sql_replies = sql_replies
        self.run_results = run_results
        self.llm_calls: list[list[ChatMessage]] = []
        self.ran: list[str] = []

    async def describe_schema(self) -> str:
        return "t(a integer)"

    async def generate_sql(self, messages: list[ChatMessage]) -> str:
        self.llm_calls.append([dict(m) for m in messages])  # type: ignore[misc]
        reply = self.sql_replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def run_read_only(self, sql: str) -> list[dict[str, Any]]:
        self.ran.append(sql)
        result = self.run_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return list(result)


def install(monkeypatch: pytest.MonkeyPatch, stubs: Stubs) -> Stubs:
    """Puts the stubs in place of the functions ask.py calls."""
    monkeypatch.setattr(ask_module, "describe_schema", stubs.describe_schema)
    monkeypatch.setattr(ask_module, "generate_sql", stubs.generate_sql)
    monkeypatch.setattr(ask_module, "run_read_only", stubs.run_read_only)
    return stubs


def test_first_query_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the first query runs, its SQL and rows are returned after one LLM call."""
    stubs = install(monkeypatch, Stubs(["SELECT a FROM t"], [[{"a": 1}]]))
    assert run(ask("how many?")) == AskResult(sql="SELECT a FROM t", rows=[{"a": 1}])
    assert len(stubs.llm_calls) == 1
    assert stubs.llm_calls[0][1]["content"] == "Schema:\nt(a integer)\n\nQuestion: how many?"
    assert stubs.ran == ["SELECT a FROM t"]


def test_rejected_query_is_corrected_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejected query goes back to the LLM as an assistant message plus the error, after the
    original messages, and the fix runs.
    """
    stubs = install(
        monkeypatch,
        Stubs(
            ["SELECT b FROM t", "SELECT a FROM t"],
            [psycopg.errors.UndefinedColumn('column "b" does not exist'), [{"a": 2}]],
        ),
    )
    assert run(ask("q")) == AskResult(sql="SELECT a FROM t", rows=[{"a": 2}])
    retry_messages = stubs.llm_calls[1]
    assert retry_messages[-2:] == [
        {"role": "assistant", "content": "SELECT b FROM t"},
        {
            "role": "user",
            "content": 'PostgreSQL rejected that query: column "b" does not exist\n'
            "Return a corrected query.",
        },
    ]
    assert retry_messages[:2] == stubs.llm_calls[0]
    assert stubs.ran == ["SELECT b FROM t", "SELECT a FROM t"]


def test_second_failure_is_raised_without_a_third_try(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second failure is raised as is; the LLM is not asked a third time."""
    stubs = install(
        monkeypatch,
        Stubs(
            ["bad 1", "bad 2", "never"],
            [psycopg.errors.SyntaxError("first"), psycopg.errors.SyntaxError("second")],
        ),
    )
    with pytest.raises(psycopg.errors.SyntaxError, match=r"^second$"):
        run(ask("q"))
    assert len(stubs.llm_calls) == 2
    assert stubs.ran == ["bad 1", "bad 2"]


def test_error_without_message_uses_its_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """An error with no message is reported to the LLM by its type name."""
    stubs = install(monkeypatch, Stubs(["x", "y"], [psycopg.errors.DataError(), []]))
    run(ask("q"))
    assert stubs.llm_calls[1][-1]["content"].startswith(
        "PostgreSQL rejected that query: DataError\n"
    )


@pytest.mark.parametrize(
    "error",
    [
        psycopg.OperationalError("connection refused"),
        psycopg.InterfaceError("the connection is closed"),
        psycopg.errors.QueryCanceled("canceling statement due to statement timeout"),
        RuntimeError("not from the database"),
    ],
)
def test_non_sql_errors_are_not_retried(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """Connection failures, timeouts and non-database errors are raised without asking the LLM
    again.
    """
    stubs = install(monkeypatch, Stubs(["SELECT 1", "never"], [error]))
    with pytest.raises(type(error)):
        run(ask("q"))
    assert len(stubs.llm_calls) == 1


def test_llm_failure_is_raised_before_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """An LLM error on the first call is raised, and nothing runs on the database."""
    stubs = install(monkeypatch, Stubs([LlmError("LLM returned no SQL")], []))
    with pytest.raises(LlmError, match="LLM returned no SQL"):
        run(ask("q"))
    assert stubs.ran == []


def test_llm_failure_on_retry_is_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """An LLM error while asking for the correction is raised."""
    install(monkeypatch, Stubs(["bad", LlmError("down")], [psycopg.errors.SyntaxError("x")]))
    with pytest.raises(LlmError, match="down"):
        run(ask("q"))
