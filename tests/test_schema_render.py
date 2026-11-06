"""schema.py without a database: identifier quoting, rendering and note matching."""

import pytest

from postgres_mcp.schema import (
    Column,
    Table,
    _one_line,
    ident,
    render_schema,
    table_ref,
    unmatched_descriptions,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("customers", "customers"),
        ("_private", "_private"),
        ("a_b$1", "a_b$1"),
        ("Customers", '"Customers"'),
        ("1st", '"1st"'),
        ("Mixed Case", '"Mixed Case"'),
        ("a-b", '"a-b"'),
        ('say "hi"', '"say ""hi"""'),
        ("trailing\n", '"trailing\n"'),
        ("", '""'),
        ("שם", '"שם"'),
    ],
)
def test_ident(name: str, expected: str) -> None:
    """Lowercase names stay bare; any other name is double-quoted, with inner quotes doubled."""
    assert ident(name) == expected


@pytest.mark.parametrize(
    ("schema_name", "table", "expected"),
    [
        ("public", "customers", "customers"),
        ("public", "Mixed Case", '"Mixed Case"'),
        ("sales", "orders", "sales.orders"),
        ("Sales", "Orders", '"Sales"."Orders"'),
    ],
)
def test_table_ref(schema_name: str, table: str, expected: str) -> None:
    """Tables in public are named alone; others as schema.table, each part quoted as needed."""
    assert table_ref(schema_name, table) == expected


def test_one_line() -> None:
    """_one_line turns every run of whitespace into one space and trims the ends."""
    assert _one_line("  a\n b\t\tc  \r\n") == "a b c"


TABLES = [
    Table(
        "public",
        "customers",
        [Column("id", "integer", ["PK"]), Column("status", "character"), Column("City", "text")],
    ),
    Table(
        "sales",
        "orders",
        [
            Column("id", "bigint", ["PK"]),
            Column("customer_id", "integer", ["→ customers.id"]),
            Column("order_id", "bigint", ["PK", "→ sales.orders.id"]),
        ],
    ),
]


def test_render_without_notes() -> None:
    """Each table is one line, name(column type keys, ...), with identifiers quoted as needed."""
    assert render_schema(TABLES, {}) == (
        'customers(id integer PK, status character, "City" text)\n'
        "sales.orders(id bigint PK, customer_id integer → customers.id, "
        "order_id bigint PK → sales.orders.id)"
    )


def test_render_with_notes_in_column_order() -> None:
    """Notes follow their table line as `-- ` comments flattened to one line, columns in table
    order.

    Blank notes and notes for unknown columns are left out.
    """
    notes = {
        "customers": {
            "description": "People\nwho   buy. Hebrew: לקוחות",
            "columns": {"City": "  ", "status": "A = active,\nC = closed", "missing": "ignored"},
        },
        "sales.orders": {"description": "  ", "columns": {"customer_id": "buyer"}},
    }
    assert render_schema(TABLES, notes) == (
        'customers(id integer PK, status character, "City" text)\n'
        "  -- customers: People who buy. Hebrew: לקוחות\n"
        "  -- status: A = active, C = closed\n"
        "sales.orders(id bigint PK, customer_id integer → customers.id, "
        "order_id bigint PK → sales.orders.id)\n"
        "  -- customer_id: buyer"
    )


def test_render_empty() -> None:
    """No tables give an empty string, whatever the notes."""
    assert render_schema([], {"t": {"description": "x"}}) == ""


def test_unmatched_descriptions() -> None:
    """Notes for tables or columns that do not exist are listed, in the file's order."""
    notes = {
        "customers": {"columns": {"status": "x", "nope": "y"}},
        "orders": {"description": "public.orders does not exist"},
        "sales.orders": {"description": "fine"},
        "sales.nothing": {},
    }
    assert unmatched_descriptions(TABLES, notes) == [
        'column "customers.nope"',
        'table "orders"',
        'table "sales.nothing"',
    ]


def test_unmatched_descriptions_none() -> None:
    """When every note matches, nothing is listed."""
    assert unmatched_descriptions(TABLES, {"customers": {"columns": {"City": "c"}}}) == []
