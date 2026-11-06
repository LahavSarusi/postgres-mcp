"""output.py: JSON conversion, error text, UTF-8 output and the event loop."""

import asyncio
import io
import ipaddress
import json
import sys
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest

from postgres_mcp import output


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, True),
        (5, 5),
        (2**70, 2**70),
        ("text", "text"),
        (1.5, 1.5),
        (float("nan"), None),
        (float("inf"), None),
        (float("-inf"), None),
        (Decimal("1234567890.12"), "1234567890.12"),
        (Decimal("1.50"), "1.50"),
        (Decimal("NaN"), "NaN"),
        (datetime(2026, 11, 1, 10, 0, tzinfo=UTC), "2026-11-01T10:00:00+00:00"),
        (date(2026, 11, 1), "2026-11-01"),
        (time(13, 45), "13:45:00"),
        (b"\xde\xad", "\\xdead"),
        (bytearray(b"\x01"), "\\x01"),
        (memoryview(b"\xff"), "\\xff"),
        (uuid.UUID(int=1), "00000000-0000-0000-0000-000000000001"),
        (timedelta(days=1, hours=2), "1 day, 2:00:00"),
        (ipaddress.ip_address("10.0.0.1"), "10.0.0.1"),
        ((1, Decimal("2")), [1, "2"]),
        ({"a": [Decimal("1"), {"b": float("nan")}], 3: "x"}, {"a": ["1", {"b": None}], "3": "x"}),
    ],
)
def test_jsonable(value: Any, expected: Any) -> None:
    """Database values become JSON values.

    NaN and infinities become null, Decimal exact text, dates ISO 8601, bytes PostgreSQL's hex text,
    other types str; containers are converted recursively, with string keys.
    """
    assert output._jsonable(value) == expected


def test_to_json_is_indented_valid_json_with_raw_unicode() -> None:
    """to_json writes JSON indented by 2 spaces, with Hebrew left unescaped and NaN as null."""
    text = output.to_json([{"name": "דנה", "n": Decimal("1.0"), "f": float("nan")}])
    assert text == '[\n  {\n    "name": "דנה",\n    "n": "1.0",\n    "f": null\n  }\n]'
    assert json.loads(text)[0]["f"] is None


def test_to_json_empty_list() -> None:
    """No rows give `[]`."""
    assert output.to_json([]) == "[]"


def test_error_text() -> None:
    """error_text is the exception's message, or its type name when it has none."""
    assert output.error_text(ValueError("bad value")) == "bad value"
    assert output.error_text(TimeoutError()) == "TimeoutError"


def test_use_utf8_output_writes_utf8_and_lf(monkeypatch: pytest.MonkeyPatch) -> None:
    """On cp1252 streams that translate newlines to CRLF (Windows with piped output), stdout and
    stderr switch to UTF-8 and LF.
    """
    out_bytes, err_bytes = io.BytesIO(), io.BytesIO()
    # As on Windows with piped output: an ANSI code page and "\r\n" translation.
    stdout = io.TextIOWrapper(out_bytes, encoding="cp1252", newline="\r\n")
    stderr = io.TextIOWrapper(err_bytes, encoding="cp1252", newline="\r\n")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    output.use_utf8_output()
    print("שלום")
    print("SQL:\nx", file=sys.stderr)
    stdout.flush()
    stderr.flush()
    assert out_bytes.getvalue() == "שלום\n".encode()
    assert err_bytes.getvalue() == b"SQL:\nx\n"


def test_use_utf8_output_leaves_other_streams_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Streams that cannot be reconfigured, such as StringIO, are left as they are, without an
    error.
    """
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)
    output.use_utf8_output()  # must not fail
    assert sys.stdout is stream


def test_run_returns_result_on_a_selector_loop_on_windows() -> None:
    """run() returns the coroutine's result and, on Windows, uses a selector event loop (psycopg's
    async mode needs one).
    """

    async def loop_type() -> type[asyncio.AbstractEventLoop]:
        return type(asyncio.get_running_loop())

    loop_class = output.run(loop_type())
    if sys.platform == "win32":
        assert issubclass(loop_class, asyncio.SelectorEventLoop)


def test_run_propagates_exceptions() -> None:
    """An exception in the coroutine comes out of run() unchanged."""

    async def fail() -> None:
        raise LookupError("boom")

    with pytest.raises(LookupError, match="boom"):
        output.run(fail())
