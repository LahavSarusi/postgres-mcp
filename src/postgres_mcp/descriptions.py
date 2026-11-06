"""Loads descriptions.json: optional, human-written notes on tables and columns.

The LLM receives these notes with the schema. Names alone cannot tell a model
what a coded value means ("status = 'A'"), which of two similar tables to use,
what unit an amount is in, or which English column a Hebrew word refers to;
these notes do. They live in a project file rather than as Postgres COMMENTs so
they can be written without rights to change the database. The file is
optional, and a malformed one fails loudly, naming the file and the bad entry,
rather than being silently ignored; pydantic does the checking.
"""

from typing import TypedDict

from pydantic import ConfigDict, TypeAdapter, ValidationError, with_config
from pydantic_core import ErrorDetails

from postgres_mcp.config import PROJECT_ROOT

descriptions_file = PROJECT_ROOT / "descriptions.json"


@with_config(ConfigDict(extra="forbid", strict=True))
class TableDescription(TypedDict, total=False):
    description: str
    columns: dict[str, str]


Descriptions = dict[str, TableDescription]
"""Table key -> its notes. Keys are "table" for public, "schema.table" otherwise."""

_validator = TypeAdapter(Descriptions)


class DescriptionsError(ValueError):
    """descriptions.json exists but is malformed."""


def _problem(error: ErrorDetails) -> str:
    """One validation error as text naming the entry: '"t".columns.c: Input should be ...'."""
    if error["type"] == "json_invalid":
        return f"not valid JSON ({error['msg']})"
    if not error["loc"]:
        return "must be a JSON object keyed by table name"
    table, *path = (str(part) for part in error["loc"])
    where = ".".join([f'"{table}"', *path])
    if error["type"] == "extra_forbidden":
        return f'{where}: unknown key; use "description" and "columns"'
    return f"{where}: {error['msg']}"


def load_descriptions() -> Descriptions:
    """Reads and checks descriptions.json; an absent file means no descriptions."""
    if not descriptions_file.exists():
        return {}
    try:
        # utf-8-sig also accepts a file saved with a byte-order mark.
        return _validator.validate_json(descriptions_file.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError) as error:
        raise DescriptionsError(f"{descriptions_file}: not valid JSON ({error})") from error
    except ValidationError as error:
        problems = "; ".join(_problem(detail) for detail in error.errors())
        raise DescriptionsError(f"{descriptions_file}: {problems}") from None


def table_key(schema: str, table: str) -> str:
    """The key a table has in descriptions.json."""
    return table if schema == "public" else f"{schema}.{table}"
