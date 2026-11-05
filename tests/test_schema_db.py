"""schema.py against the seeded database: reading the catalog and the cached description."""

from pathlib import Path

import pytest
from conftest import Database

from postgres_mcp import schema
from postgres_mcp.descriptions import DescriptionsError
from postgres_mcp.output import run
from postgres_mcp.schema import Column, Table, describe_schema, read_schema


def tables_by_key(tables: list[Table]) -> dict[str, Table]:
    return {f"{t.schema}.{t.name}": t for t in tables}


def test_reads_every_user_table_and_view(connected: Database) -> None:
    tables = tables_by_key(run(read_schema()))
    assert sorted(tables) == [
        "public.Mixed Case",
        "public.active_customers",
        "public.customers",
        "public.types_demo",
        "sales.order_items",
        "sales.orders",
    ]


def test_columns_in_table_order_with_types(connected: Database) -> None:
    customers = tables_by_key(run(read_schema()))["public.customers"]
    assert customers.columns == [
        Column("id", "integer", ["PK"]),
        Column("name", "text", []),
        Column("city", "text", []),
        Column("status", "character", []),
    ]


def test_foreign_keys_and_composite_primary_keys(connected: Database) -> None:
    tables = tables_by_key(run(read_schema()))
    orders = {c.name: c for c in tables["sales.orders"].columns}
    assert orders["customer_id"].keys == ["→ customers.id"]
    assert orders["id"].keys == ["PK"]
    assert orders["Weird Col"].type == "text"

    items = {c.name: c for c in tables["sales.order_items"].columns}
    assert sorted(items["order_id"].keys) == ["PK", "→ sales.orders.id"]
    assert items["line"].keys == ["PK"]
    assert items["qty"].keys == []


def test_views_have_no_keys(connected: Database) -> None:
    view = tables_by_key(run(read_schema()))["public.active_customers"]
    assert [c.name for c in view.columns] == ["id", "name", "city", "status"]
    assert all(c.keys == [] for c in view.columns)


def test_describe_schema_text(connected: Database) -> None:
    lines = run(describe_schema()).splitlines()
    assert "customers(id integer PK, name text, city text, status character)" in lines
    assert '"Mixed Case"(k integer PK, v text)' in lines
    orders = next(line for line in lines if line.startswith("sales.orders("))
    assert "customer_id integer → customers.id" in orders
    assert '"Weird Col" text' in orders


def test_describe_schema_adds_notes_and_warns_about_unknown_ones(
    connected: Database, isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    isolated.write_text(
        '{"customers": {"description": "People. Hebrew: לקוחות",'
        ' "columns": {"status": "A = active", "nope": "x"}},'
        ' "sales.orders": {"columns": {"total": "agorot"}},'
        ' "ghost": {"description": "gone"}}',
        encoding="utf-8",
    )
    text = run(describe_schema())
    assert (
        "customers(id integer PK, name text, city text, status character)\n"
        "  -- customers: People. Hebrew: לקוחות\n"
        "  -- status: A = active\n"
    ) in text
    assert "  -- total: agorot" in text
    assert "ghost" not in text
    assert capsys.readouterr().err == (
        f'Warning: {isolated} describes column "customers.nope", which does not exist.\n'
        f'Warning: {isolated} describes table "ghost", which does not exist.\n'
    )


def test_describe_schema_is_cached(connected: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    first = run(describe_schema())

    async def no_database() -> None:
        raise AssertionError("the cached description should not touch the database")

    monkeypatch.setattr(schema, "open_connection", no_database)
    assert run(describe_schema()) is first


def test_malformed_descriptions_stop_describe_schema(connected: Database, isolated: Path) -> None:
    isolated.write_text("[]", encoding="utf-8")
    with pytest.raises(DescriptionsError):
        run(describe_schema())
