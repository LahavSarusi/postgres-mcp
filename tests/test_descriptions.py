"""descriptions.py: loading and checking descriptions.json."""

from pathlib import Path

import pytest
from conftest import PROJECT_ROOT

from postgres_mcp import descriptions
from postgres_mcp.descriptions import DescriptionsError, load_descriptions, table_key


def test_absent_file_means_no_descriptions(isolated: Path) -> None:
    """Without descriptions.json there are no notes."""
    assert not isolated.exists()
    assert load_descriptions() == {}


def test_valid_file(isolated: Path) -> None:
    """A valid file loads as written, including entries without a description or columns."""
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
    """`{}` is a valid file with no notes."""
    isolated.write_text("{}", encoding="utf-8")
    assert load_descriptions() == {}


def test_byte_order_mark_is_accepted(isolated: Path) -> None:
    """A UTF-8 byte order mark at the start, as some Windows editors write, is ignored."""
    isolated.write_text('﻿{"t": {"description": "d"}}', encoding="utf-8")
    assert load_descriptions() == {"t": {"description": "d"}}


def test_starter_file_blanks_load(isolated: Path) -> None:
    """The blank strings of the starter file load without error."""
    isolated.write_text('{"t": {"description": "", "columns": {"c": ""}}}', encoding="utf-8")
    assert load_descriptions() == {"t": {"description": "", "columns": {"c": ""}}}


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("[]", "must be a JSON object keyed by table name"),
        ('"text"', "must be a JSON object keyed by table name"),
        ('{"t": 1}', '"t": Input should be an object'),
        ('{"t": []}', '"t": Input should be an object'),
        (
            '{"t": {"desc": "x", "other": 1}}',
            '"t".desc: unknown key; use "description" and "columns"; '
            '"t".other: unknown key; use "description" and "columns"',
        ),
        ('{"t": {"description": 5}}', '"t".description: Input should be a valid string'),
        ('{"t": {"description": null}}', '"t".description: Input should be a valid string'),
        ('{"t": {"columns": []}}', '"t".columns: Input should be an object'),
        ('{"t": {"columns": "c"}}', '"t".columns: Input should be an object'),
        ('{"t": {"columns": {"c": 1}}}', '"t".columns.c: Input should be a valid string'),
        ('{"t": {"columns": {"c": null}}}', '"t".columns.c: Input should be a valid string'),
    ],
)
def test_malformed_entries_name_the_file_and_entry(
    isolated: Path, content: str, message: str
) -> None:
    """Each kind of malformed entry raises an error naming the file, the entry and what is wrong."""
    isolated.write_text(content, encoding="utf-8")
    with pytest.raises(DescriptionsError) as error:
        load_descriptions()
    assert str(error.value) == f"{isolated}: {message}"


@pytest.mark.parametrize("raw", [b"{bad", b'{"t": }', b"", b"\xff\xfe\x00"])
def test_unparsable_file(isolated: Path, raw: bytes) -> None:
    """Broken JSON, an empty file and non-UTF-8 bytes are reported as not valid JSON."""
    isolated.write_bytes(raw)
    with pytest.raises(DescriptionsError) as error:
        load_descriptions()
    assert str(error.value).startswith(f"{isolated}: not valid JSON (")


def test_unreadable_path_is_reported(isolated: Path) -> None:
    """A path that cannot be read, here a directory, raises DescriptionsError."""
    isolated.mkdir()  # a directory where the file should be
    with pytest.raises(DescriptionsError, match="not valid JSON"):
        load_descriptions()


def test_descriptions_error_is_a_value_error() -> None:
    """DescriptionsError is a ValueError."""
    assert issubclass(DescriptionsError, ValueError)


def test_default_file_location() -> None:
    """descriptions.json belongs at the project root."""
    # descriptions_file itself is redirected to tmp_path by the `isolated` fixture;
    # the real one is <project root>/descriptions.json.
    assert descriptions.PROJECT_ROOT == PROJECT_ROOT


@pytest.mark.parametrize(
    ("schema_name", "table", "key"),
    [("public", "customers", "customers"), ("sales", "orders", "sales.orders")],
)
def test_table_key(schema_name: str, table: str, key: str) -> None:
    """Keys are the bare name for public tables and schema.table otherwise."""
    assert table_key(schema_name, table) == key
