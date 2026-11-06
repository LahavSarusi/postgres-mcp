"""Command-line entry point: `postgres-mcp-ask "<question>"`.

It lets a person or another program use the free-text-to-rows flow without an
MCP client. The rows go to stdout as a JSON array, so the output can be piped
or parsed; the generated SQL and any error go to stderr, so they never mix with
the JSON.
"""

import sys

from postgres_mcp.ask import ask
from postgres_mcp.db import connect
from postgres_mcp.output import exit_on_error, run, to_json, use_utf8_output


def main() -> None:
    use_utf8_output()
    question = " ".join(sys.argv[1:]).strip()
    if not question:
        print('Usage: postgres-mcp-ask "<your question>"', file=sys.stderr)
        sys.exit(1)

    with exit_on_error():
        connect()
        result = run(ask(question))
    print(f"SQL:\n{result.sql}\n", file=sys.stderr)
    print(to_json(result.rows))


if __name__ == "__main__":
    main()
