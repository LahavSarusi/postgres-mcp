"""cli.py run as the installed postgres-mcp-ask command."""

import json
import sys
from collections.abc import Coroutine
from typing import Any

import pytest
from conftest import Database, MockLlm, Reply, run_script, script_path

from postgres_mcp import cli

ASK = "postgres-mcp-ask"


def test_ctrl_c_exits_130(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(coroutine: Coroutine[Any, Any, Any]) -> None:
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(sys, "argv", ["postgres-mcp-ask", "q"])
    monkeypatch.setattr(cli, "connect", lambda: None)
    monkeypatch.setattr(cli, "run", interrupted)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 130


def test_rows_on_stdout_sql_on_stderr(pg: Database, llm: MockLlm) -> None:
    llm.script("SELECT name, city FROM customers ORDER BY id")
    result = run_script([script_path(ASK), "מי", "הלקוחות?"])
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    # UTF-8 with "\n" line ends even when piped on Windows.
    assert b"\r\n" not in result.stdout and b"\r\n" not in result.stderr
    rows = json.loads(result.stdout.decode("utf-8"))
    assert rows[0] == {"name": "דנה כהן", "city": "חיפה"}
    assert len(rows) == 3
    assert result.stdout.decode("utf-8").startswith('[\n  {\n    "name": "דנה כהן"')
    assert result.stderr.decode("utf-8") == (
        "SQL:\nSELECT name, city FROM customers ORDER BY id\n\n"
    )
    assert llm.questions[0].endswith("\n\nQuestion: מי הלקוחות?")


def test_retry_shows_the_corrected_sql(pg: Database, llm: MockLlm) -> None:
    llm.script("SELECT nam FROM customers", "SELECT count(*)::int AS n FROM customers")
    result = run_script([script_path(ASK), "how many"])
    assert result.returncode == 0
    assert json.loads(result.stdout) == [{"n": 3}]
    assert result.stderr.decode() == "SQL:\nSELECT count(*)::int AS n FROM customers\n\n"


def test_usage_without_a_question() -> None:
    for args in ([], ["   "], ["", ""]):
        result = run_script([script_path(ASK), *args])
        assert result.returncode == 1
        assert result.stdout == b""
        assert result.stderr.decode().strip() == 'Usage: postgres-mcp-ask "<your question>"'


def test_error_goes_to_stderr_with_exit_code_1(pg: Database, llm: MockLlm) -> None:
    llm.script(Reply(status=500, raw=b"model overloaded"))
    result = run_script([script_path(ASK), "q"])
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr.decode().strip() == "LLM request failed (500): model overloaded"


def test_missing_database_settings(llm: MockLlm) -> None:
    result = run_script([script_path(ASK), "q"])
    assert result.returncode == 1
    assert result.stderr.decode().startswith(
        "Missing database settings: PGHOST, PGUSER, PGPASSWORD, PGDATABASE."
    )
    assert llm.requests == []


def test_missing_llm_settings(pg: Database) -> None:
    result = run_script([script_path(ASK), "q"], {"LLM_MODEL": ""})
    assert result.returncode == 1
    assert result.stderr.decode().startswith("Missing LLM settings: LLM_MODEL.")


def test_invalid_limit_setting() -> None:
    result = run_script([script_path(ASK), "q"], {"MAX_ROWS": "lots"})
    assert result.returncode == 1
    assert result.stderr.decode().strip() == 'MAX_ROWS must be a positive whole number, got "lots".'
