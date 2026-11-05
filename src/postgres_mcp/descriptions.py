"""Loads descriptions.json: optional, human-written notes on tables and columns.

The LLM receives these notes with the schema. Names alone cannot tell a model
what a coded value means ("status = 'A'"), which of two similar tables to use,
what unit an amount is in, or which English column a Hebrew word refers to;
these notes do. They live in a project file rather than as Postgres COMMENTs so
they can be written without rights to change the database. The file is
optional, and a malformed one fails loudly, naming the file and the bad entry,
rather than being silently ignored.
"""

import json
from typing import Any, NoReturn, TypedDict

from postgres_mcp.config import PROJECT_ROOT

descriptions_file = PROJECT_ROOT / "descriptions.json"


class TableDescription(TypedDict, total=False):
    description: str
    columns: dict[str, str]


Descriptions = dict[str, TableDescription]
"""Table key -> its notes. Keys are "table" for public, "schema.table" otherwise."""


class DescriptionsError(ValueError):
    """descriptions.json exists but is malformed."""


def _fail(detail: str) -> NoReturn:
    raise DescriptionsError(f"{descriptions_file}: {detail}")


def load_descriptions() -> Descriptions:
    """Reads and checks descriptions.json; an absent file means no descriptions."""
    if not descriptions_file.exists():
        return {}

    try:
        # utf-8-sig also accepts a file saved with a byte-order mark.
        parsed: Any = json.loads(descriptions_file.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail(f"not valid JSON ({error})")
    if not isinstance(parsed, dict):
        _fail("must be a JSON object keyed by table name")

    result: Descriptions = {}
    for table, entry in parsed.items():
        if not isinstance(entry, dict):
            _fail(f'"{table}" must be an object')
        extra = [key for key in entry if key not in ("description", "columns")]
        if extra:
            _fail(
                f'"{table}" has unknown key(s) {", ".join(extra)}; use "description" and "columns"'
            )
        notes: TableDescription = {}
        if "description" in entry:
            description = entry["description"]
            if not isinstance(description, str):
                _fail(f'"{table}".description must be a string')
            notes["description"] = description
        if "columns" in entry:
            columns = entry["columns"]
            if not isinstance(columns, dict):
                _fail(f'"{table}".columns must be an object of column name → text')
            for column, text in columns.items():
                if not isinstance(text, str):
                    _fail(f'"{table}".columns.{column} must be a string')
            notes["columns"] = columns
        result[table] = notes
    return result


def table_key(schema: str, table: str) -> str:
    """The key a table has in descriptions.json."""
    return table if schema == "public" else f"{schema}.{table}"
