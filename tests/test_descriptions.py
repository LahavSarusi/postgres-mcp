"""descriptions.py: loading and checking descriptions.json."""

from pathlib import Path

import pytest
from conftest import PROJECT_ROOT

from postgres_mcp import descriptions
from postgres_mcp.descriptions import DescriptionsError, load_descriptions, table_key


def test_absent_file_means_no_descriptions(isolated: Path) -> None:
    assert not isolated.exists()
    assert load_descriptions() == {}


def test_valid_file(isolated: Path) -> None:
    isolated.write_text(
        '{"customers": {"description": "People. Hebrew: לקוחות",'
        ' "columns": {"status": "A = active"}},'
        ' "sales.orders": {"columns": {}}, "empty": {}}',
        encoding="utf-8",
    )
    assert load_descriptions() == {
        "customers": {"description": "People. Hebrew: לקוחות", "columns": {"status": "A = active"}},
        "sales.orders": {"columns": {}},
        "empty": {},
    }


def test_empty_object(isolated: Path) -> None:
    isolated.write_text("{}", encoding="utf-8")
    assert load_descriptions() == {}


def test_byte_order_mark_is_accepted(isolated: Path) -> None:
    isolated.write_text('﻿{"t": {"description": "d"}}', encoding="utf-8")
    assert load_descriptions() == {"t": {"description": "d"}}


def test_starter_file_blanks_load(isolated: Path) -> None:
    isolated.write_text('{"t": {"description": "", "columns": {"c": ""}}}', encoding="utf-8")
    assert load_descriptions() == {"t": {"description": "", "columns": {"c": ""}}}


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("[]", "must be a JSON object keyed by table name"),
        ('"text"', "must be a JSON object keyed by table name"),
        ('{"t": 1}', '"t" must be an object'),
        ('{"t": []}', '"t" must be an object'),
        (
            '{"t": {"desc": "x", "other": 1}}',
            '"t" has unknown key(s) desc, other; use "description" and "columns"',
        ),
        ('{"t": {"description": 5}}', '"t".description must be a string'),
        ('{"t": {"description": null}}', '"t".description must be a string'),
        ('{"t": {"columns": []}}', '"t".columns must be an object of column name → text'),
        ('{"t": {"columns": "c"}}', '"t".columns must be an object of column name → text'),
        ('{"t": {"columns": {"c": 1}}}', '"t".columns.c must be a string'),
        ('{"t": {"columns": {"c": null}}}', '"t".columns.c must be a string'),
    ],
)
def test_malformed_entries_name_the_file_and_entry(
    isolated: Path, content: str, message: str
) -> None:
    isolated.write_text(content, encoding="utf-8")
    with pytest.raises(DescriptionsError) as error:
        load_descriptions()
    assert str(error.value) == f"{isolated}: {message}"


@pytest.mark.parametrize("raw", [b"{bad", b'{"t": }', b"", b"\xff\xfe\x00"])
def test_unparsable_file(isolated: Path, raw: bytes) -> None:
    isolated.write_bytes(raw)
    with pytest.raises(DescriptionsError) as error:
        load_descriptions()
    assert str(error.value).startswith(f"{isolated}: not valid JSON (")


def test_unreadable_path_is_reported(isolated: Path) -> None:
    isolated.mkdir()  # a directory where the file should be
    with pytest.raises(DescriptionsError, match="not valid JSON"):
        load_descriptions()


def test_descriptions_error_is_a_value_error() -> None:
    assert issubclass(DescriptionsError, ValueError)


def test_default_file_location() -> None:
    # descriptions_file itself is redirected to tmp_path by the `isolated` fixture;
    # the real one is <project root>/descriptions.json.
    assert descriptions.PROJECT_ROOT == PROJECT_ROOT


@pytest.mark.parametrize(
    ("schema_name", "table", "key"),
    [("public", "customers", "customers"), ("sales", "orders", "sales.orders")],
)
def test_table_key(schema_name: str, table: str, key: str) -> None:
    assert table_key(schema_name, table) == key
