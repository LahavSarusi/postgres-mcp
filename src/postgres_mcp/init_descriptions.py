"""`postgres-mcp-descriptions-init`: writes a starter descriptions.json.

The file lists every table and column with empty notes, so the exact names
never have to be typed by hand; the user fills in only the ambiguous entries
and empty ones are ignored. It never overwrites an existing file, since that
file holds the user's work.
"""

import json
import sys

from postgres_mcp.db import connect
from postgres_mcp.descriptions import Descriptions, descriptions_file, table_key
from postgres_mcp.output import exit_on_error, run, use_utf8_output
from postgres_mcp.schema import read_schema


def main() -> None:
    use_utf8_output()
    if descriptions_file.exists():
        print(f"{descriptions_file} already exists; not overwriting it.", file=sys.stderr)
        sys.exit(1)

    with exit_on_error():
        connect()
        starter: Descriptions = {
            table_key(table.schema, table.name): {
                "description": "",
                "columns": {column.name: "" for column in table.columns},
            }
            for table in run(read_schema())
        }
        # "x" refuses to overwrite a file created since the check above.
        with descriptions_file.open("x", encoding="utf-8", newline="\n") as file:
            json.dump(starter, file, indent=2, ensure_ascii=False)
            file.write("\n")
    print(f"Wrote {descriptions_file} with {len(starter)} tables.", file=sys.stderr)


if __name__ == "__main__":
    main()
