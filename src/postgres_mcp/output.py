"""Shared helpers for the entry points: JSON output, error handling, and running async code.

Rows from Postgres hold values JSON cannot encode directly (Decimal, dates,
UUIDs, bytes); they are converted to the text Postgres itself would show, so a
numeric keeps every digit. Non-finite floats become null, as in JavaScript.
"""

import asyncio
import io
import json
import math
import sys
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, TypeVar

T = TypeVar("T")


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(value).hex()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return str(value)  # UUID, timedelta, IP addresses, ranges, ...


def to_json(value: Any) -> str:
    """JSON text indented by two spaces; non-ASCII (Hebrew) is kept as is."""
    return json.dumps(_jsonable(value), indent=2, ensure_ascii=False)


def error_text(error: BaseException) -> str:
    """An exception's message, or its type name when it has none."""
    return str(error) or type(error).__name__


@contextmanager
def exit_on_error() -> Iterator[None]:
    """For the entry points: any error is printed to stderr and exits with code 1.

    Ctrl+C exits with code 130, the shell convention for SIGINT.
    """
    try:
        yield
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as error:
        print(error_text(error), file=sys.stderr)
        sys.exit(1)


def use_utf8_output() -> None:
    """Writes stdout and stderr as UTF-8 with "\\n" line ends.

    Windows otherwise encodes piped output in the ANSI code page, which cannot
    hold Hebrew and would fail mid-print, and turns each "\\n" into "\\r\\n".
    """
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", newline="\n")


def run(main: Coroutine[Any, Any, T]) -> T:
    """Runs a coroutine to completion.

    psycopg's async mode cannot use Windows' default Proactor event loop, so a
    selector loop is used there.
    """
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=factory) as runner:
        return runner.run(main)
