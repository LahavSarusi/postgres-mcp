"""Describes the database's tables to the LLM.

The LLM can only write correct SQL for tables and columns it has been told
about. Every user schema is covered (not only "public"), with primary and
foreign keys, because the keys are what let the model write correct joins;
human-written notes from descriptions.json are added under the tables they
describe. The output is compact text, one line per table plus a line per note,
to keep the prompt small. It is cached: the schema rarely changes while the
server runs, and reading it costs two catalog queries.
"""

import re
import sys
from dataclasses import dataclass, field

from postgres_mcp.db import open_connection
from postgres_mcp.descriptions import (
    Descriptions,
    descriptions_file,
    load_descriptions,
    table_key,
)

# Run without parameters, so psycopg leaves the "%" in LIKE patterns alone.
COLUMNS_SQL = r"""
  SELECT c.table_schema, c.table_name, c.column_name, c.data_type
  FROM information_schema.columns c
  JOIN information_schema.tables t
    ON t.table_schema = c.table_schema AND t.table_name = c.table_name
  WHERE t.table_type IN ('BASE TABLE', 'VIEW')
    AND c.table_schema NOT IN ('pg_catalog', 'information_schema')
    AND c.table_schema NOT LIKE 'pg\_%'
  ORDER BY c.table_schema, c.table_name, c.ordinal_position"""

KEYS_SQL = """
  SELECT n.nspname AS table_schema, cl.relname AS table_name, a.attname AS column_name,
         con.contype, fn.nspname AS ref_schema, fcl.relname AS ref_table, fa.attname AS ref_column
  FROM pg_constraint con
  JOIN pg_class cl ON cl.oid = con.conrelid
  JOIN pg_namespace n ON n.oid = cl.relnamespace
  CROSS JOIN LATERAL unnest(con.conkey, con.confkey) AS k(attnum, ref_attnum)
  JOIN pg_attribute a ON a.attrelid = cl.oid AND a.attnum = k.attnum
  LEFT JOIN pg_class fcl ON fcl.oid = con.confrelid
  LEFT JOIN pg_namespace fn ON fn.oid = fcl.relnamespace
  LEFT JOIN pg_attribute fa ON fa.attrelid = con.confrelid AND fa.attnum = k.ref_attnum
  WHERE con.contype IN ('p', 'f')
    AND n.nspname NOT IN ('pg_catalog', 'information_schema')"""


@dataclass
class Column:
    name: str
    type: str
    keys: list[str] = field(default_factory=list)
    """"PK" and/or "→ other_table.column"."""


@dataclass
class Table:
    schema: str
    name: str
    columns: list[Column] = field(default_factory=list)


_PLAIN_IDENT = re.compile(r"[a-z_][a-z0-9_$]*")


def ident(name: str) -> str:
    """Quotes an identifier only when Postgres would need it quoted."""
    if _PLAIN_IDENT.fullmatch(name):
        return name
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def table_ref(schema: str, table: str) -> str:
    """Table name as it must appear in SQL; "public" needs no schema prefix."""
    return ident(table) if schema == "public" else f"{ident(schema)}.{ident(table)}"


async def read_schema() -> list[Table]:
    """Reads every user table and view with its columns and keys."""
    async with await open_connection() as conn:
        columns = await (await conn.execute(COLUMNS_SQL)).fetchall()
        keys = await (await conn.execute(KEYS_SQL)).fetchall()

    key_notes: dict[str, list[str]] = {}
    for key in keys:
        column_id = f"{key['table_schema']}.{key['table_name']}.{key['column_name']}"
        if key["contype"] == "p":
            note = "PK"
        else:
            target = table_ref(key["ref_schema"], key["ref_table"])
            note = f"→ {target}.{ident(key['ref_column'])}"
        key_notes.setdefault(column_id, []).append(note)

    tables: dict[str, Table] = {}
    for col in columns:
        table_id = f"{col['table_schema']}.{col['table_name']}"
        table = tables.get(table_id)
        if table is None:
            table = Table(schema=col["table_schema"], name=col["table_name"])
            tables[table_id] = table
        table.columns.append(
            Column(
                name=col["column_name"],
                type=col["data_type"],
                keys=key_notes.get(f"{table_id}.{col['column_name']}", []),
            )
        )
    return list(tables.values())


def _one_line(text: str) -> str:
    """A note on one line, so a newline in the file cannot break the prompt's layout."""
    return re.sub(r"\s+", " ", text).strip()


def render_schema(tables: list[Table], descriptions: Descriptions) -> str:
    """Renders the tables as prompt text, with each table's notes beneath it."""
    blocks: list[str] = []
    for table in tables:
        key = table_key(table.schema, table.name)
        cols = [" ".join([ident(c.name), c.type, *c.keys]) for c in table.columns]
        lines = [f"{table_ref(table.schema, table.name)}({', '.join(cols)})"]

        notes = descriptions.get(key, {})
        description = notes.get("description", "")
        if description.strip():
            lines.append(f"  -- {key}: {_one_line(description)}")
        column_notes = notes.get("columns", {})
        for column in table.columns:
            text = column_notes.get(column.name, "")
            if text.strip():
                lines.append(f"  -- {column.name}: {_one_line(text)}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def unmatched_descriptions(tables: list[Table], descriptions: Descriptions) -> list[str]:
    """Description entries naming a table or column that does not exist."""
    by_key = {table_key(t.schema, t.name): t for t in tables}
    problems: list[str] = []
    for key, notes in descriptions.items():
        table = by_key.get(key)
        if table is None:
            problems.append(f'table "{key}"')
            continue
        names = {c.name for c in table.columns}
        for column in notes.get("columns", {}):
            if column not in names:
                problems.append(f'column "{key}.{column}"')
    return problems


_cached: str | None = None


async def describe_schema() -> str:
    global _cached
    if _cached is not None:
        return _cached

    tables = await read_schema()
    descriptions = load_descriptions()
    for problem in unmatched_descriptions(tables, descriptions):
        # A typo or a renamed table; the note is skipped, the question still runs.
        print(
            f"Warning: {descriptions_file} describes {problem}, which does not exist.",
            file=sys.stderr,
        )
    _cached = render_schema(tables, descriptions)
    return _cached
