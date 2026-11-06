"""init_descriptions.py: writing the starter descriptions.json."""

import json
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest
from conftest import Database

from postgres_mcp import init_descriptions
from postgres_mcp.descriptions import load_descriptions
from postgres_mcp.init_descriptions import main


def test_writes_every_table_and_column(
    pg: Database, isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Writes a blank entry for every table and view, columns in table order, as indented UTF-8 JSON
    with LF line ends that loads cleanly.
    """
    main()
    raw = isolated.read_bytes()
    assert b"\r\n" not in raw
    assert raw.endswith(b"}\n")
    data = json.loads(raw)
    assert sorted(data) == [
        "Mixed Case",
        "active_customers",
        "customers",
        "sales.order_items",
        "sales.orders",
        "types_demo",
    ]
    assert data["customers"] == {
        "description": "",
        "columns": {"id": "", "name": "", "city": "", "status": ""},
    }
    assert list(data["sales.orders"]["columns"])[-1] == "Weird Col"
    assert raw.startswith(b'{\n  "')  # two-space indent
    assert capsys.readouterr().err == f"Wrote {isolated} with 6 tables.\n"
    assert load_descriptions() == data  # the starter loads cleanly


def test_never_overwrites(pg: Database, isolated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An existing descriptions.json is left untouched, and it exits with code 1."""
    isolated.write_text('{"keep": {}}', encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == f"{isolated} already exists; not overwriting it.\n"
    assert isolated.read_text(encoding="utf-8") == '{"keep": {}}'


def test_database_error_leaves_no_file(
    pg: Database, isolated: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """A database error is printed, exits with code 1 and writes no file."""
    monkeypatch.setenv("PGPASSWORD", "wrong")
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 1
    assert "password authentication failed" in capsys.readouterr().err
    assert not isolated.exists()


def test_ctrl_c_exits_130_and_leaves_no_file(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl+C exits with code 130 and writes no file."""

    def interrupted(coroutine: Coroutine[Any, Any, Any]) -> None:
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(init_descriptions, "connect", lambda: None)
    monkeypatch.setattr(init_descriptions, "run", interrupted)
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 130
    assert not isolated.exists()


def test_missing_settings(isolated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Missing database settings exit with code 1 and write no file."""
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 1
    assert capsys.readouterr().err.startswith("Missing database settings: PGHOST, PGUSER")
    assert not isolated.exists()
