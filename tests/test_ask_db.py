"""The whole question-to-rows flow against the seeded database and the mock LLM."""

import psycopg
import pytest
from conftest import Database, MockLlm

from postgres_mcp.ask import ask
from postgres_mcp.output import run


def test_question_to_rows(connected: Database, llm: MockLlm) -> None:
    """A Hebrew question becomes SQL from the LLM and rows from the database; the prompt holds the
    system message, the schema and the question.
    """
    llm.script("SELECT name FROM customers WHERE city = 'חיפה'")
    result = run(ask("מי הלקוחות מחיפה?"))
    assert result.sql == "SELECT name FROM customers WHERE city = 'חיפה'"
    assert result.rows == [{"name": "דנה כהן"}]

    [request] = llm.requests
    system, user = request["body"]["messages"]
    assert system["role"] == "system"
    assert user["content"].startswith("Schema:\n")
    assert "customers(id integer PK, name text, city text, status character)" in user["content"]
    assert user["content"].endswith("\n\nQuestion: מי הלקוחות מחיפה?")


def test_postgres_error_goes_back_to_the_llm(connected: Database, llm: MockLlm) -> None:
    """When PostgreSQL rejects the query, the LLM gets its query and the error back, and the
    corrected query's rows are returned.
    """
    llm.script("SELECT nam FROM customers", "SELECT name FROM customers ORDER BY id LIMIT 1")
    result = run(ask("first customer"))
    assert result.rows == [{"name": "דנה כהן"}]
    retry = llm.requests[1]["body"]["messages"]
    assert retry[2] == {"role": "assistant", "content": "SELECT nam FROM customers"}
    assert retry[3]["content"].startswith(
        'PostgreSQL rejected that query: column "nam" does not exist'
    )
    assert retry[3]["content"].endswith("\nReturn a corrected query.")


def test_write_attempt_is_refused_and_retried(connected: Database, llm: MockLlm) -> None:
    """A DELETE from the LLM is refused by the database, the retried SELECT runs, and no rows are
    deleted.
    """
    llm.script("DELETE FROM customers", "SELECT count(*)::int AS n FROM customers")
    assert run(ask("delete everyone")).rows == [{"n": 3}]


def test_retry_failure_is_raised(connected: Database, llm: MockLlm) -> None:
    """If the corrected query fails too, its database error is raised after exactly two LLM
    requests.
    """
    llm.script("SELECT nam FROM customers", "SELECT still_wrong FROM customers")
    with pytest.raises(psycopg.errors.UndefinedColumn, match="still_wrong"):
        run(ask("q"))
    assert len(llm.requests) == 2
