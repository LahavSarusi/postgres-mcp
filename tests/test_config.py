"""config.py: .env loading, the numeric limits and the LLM settings."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import PROJECT_ROOT

from postgres_mcp import config

# ---------------------------------------------------------------- _positive_int


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("500", 500), (" 500 ", 500), ("1", 1), ("", 77)],
)
def test_positive_int_accepts_whole_numbers(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    """Whole numbers are accepted, also with spaces; an empty value gives the fallback."""
    monkeypatch.setenv("SOME_LIMIT", raw)
    assert config._positive_int("SOME_LIMIT", 77) == expected


def test_positive_int_unset_uses_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset variable gives the fallback."""
    monkeypatch.delenv("SOME_LIMIT", raising=False)
    assert config._positive_int("SOME_LIMIT", 77) == 77


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "1.5", "500.0", "5e2", "inf", "nan", "12abc"])
def test_positive_int_rejects_others(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], raw: str
) -> None:
    """Zero, negatives, decimals, exponents, inf, nan and text exit with code 1 and a message
    naming the variable.
    """
    monkeypatch.setenv("SOME_LIMIT", raw)
    with pytest.raises(SystemExit) as exit_info:
        config._positive_int("SOME_LIMIT", 77)
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == f'SOME_LIMIT must be a positive whole number, got "{raw}".\n'


# ---------------------------------------------------------------- llm_config


def test_llm_config_reads_settings_and_strips_trailing_slashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three LLM_* settings are read, and trailing slashes are removed from the base URL."""
    monkeypatch.setenv("LLM_BASE_URL", "https://host.example/v1///")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_MODEL", "m")
    assert config.llm_config() == config.LlmConfig("https://host.example/v1", "k", "m")


def test_llm_config_names_every_missing_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset and empty settings are all named in one error that points at .env."""
    monkeypatch.delenv("LLM_BASE_URL")
    monkeypatch.setenv("LLM_MODEL", "")
    with pytest.raises(RuntimeError) as error:
        config.llm_config()
    assert str(error.value) == (
        f"Missing LLM settings: LLM_BASE_URL, LLM_MODEL. Set them in {config.env_file}."
    )


def test_env_file_is_at_project_root() -> None:
    """.env is looked up at the project root."""
    assert config.env_file == PROJECT_ROOT / ".env"
    assert config.PROJECT_ROOT == PROJECT_ROOT


# ---------------------------------------------------------------- .env loading
# Run in a fresh interpreter on a copy of the package, so the copy's project
# root (tmp_path) holds a .env written by the test, and the current directory is
# elsewhere, as when an MCP client starts the server.

PRINT_SETTINGS = (
    "import os, postgres_mcp.config as c; "
    "print(c.max_rows, c.query_timeout_ms, os.environ.get('PGPASSWORD'), "
    "os.environ.get('PGSSLMODE'), sep='|')"
)


def run_with_env_file(
    tmp_path: Path, env_text: str | None, environ: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Imports config in a fresh interpreter from a copy of the package.

    `env_text` becomes the copy's .env (no file when None) and the working directory is elsewhere.
    The child prints max_rows|query_timeout_ms|PGPASSWORD|PGSSLMODE.
    """
    shutil.copytree(
        PROJECT_ROOT / "src" / "postgres_mcp",
        tmp_path / "src" / "postgres_mcp",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    if env_text is not None:
        (tmp_path / ".env").write_text(env_text, encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env = {"PYTHONPATH": str(tmp_path / "src"), **(environ or {})}
    for name in ("SYSTEMROOT", "PATH"):  # needed for Python to start on Windows
        if name in os.environ:
            env[name] = os.environ[name]
    return subprocess.run(
        [sys.executable, "-c", PRINT_SETTINGS],
        cwd=elsewhere, env=env, capture_output=True, text=True, timeout=60,
    )  # fmt: skip


def test_env_file_loaded_from_project_root_not_cwd(tmp_path: Path) -> None:
    """.env at the project root is read when the working directory is elsewhere; keys it lacks stay
    unset.
    """
    result = run_with_env_file(tmp_path, "MAX_ROWS=42\nQUERY_TIMEOUT_MS=900\nPGPASSWORD=pw\n")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "42|900|pw|None"


def test_environment_overrides_env_file(tmp_path: Path) -> None:
    """A variable already in the environment beats the value in .env."""
    result = run_with_env_file(tmp_path, "MAX_ROWS=42\n", {"MAX_ROWS": "7"})
    assert result.stdout.strip().split("|")[0] == "7", result.stderr


def test_defaults_without_env_file(tmp_path: Path) -> None:
    """Without a .env, MAX_ROWS is 500 and QUERY_TIMEOUT_MS is 15000."""
    result = run_with_env_file(tmp_path, None)
    assert result.stdout.strip() == "500|15000|None|None", result.stderr


def test_password_taken_literally(tmp_path: Path) -> None:
    """Values in .env are not expanded or unquoted ($, #, @ and quotes stay as written), and an
    empty value stays empty.
    """
    password = "p@ss#w0rd!${HOME}$x'\""
    result = run_with_env_file(tmp_path, f"PGPASSWORD={password}\nPGSSLMODE=\n")
    assert result.stdout.strip() == f"500|15000|{password}|", result.stderr


def test_invalid_limit_in_env_file_exits(tmp_path: Path) -> None:
    """An invalid limit in .env makes the import exit with code 1 and a message naming it."""
    result = run_with_env_file(tmp_path, "QUERY_TIMEOUT_MS=soon\n")
    assert result.returncode == 1
    assert result.stderr.strip() == 'QUERY_TIMEOUT_MS must be a positive whole number, got "soon".'
