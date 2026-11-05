# PostgreSQL MCP server (Python)

Ask your PostgreSQL database a question in plain language (Hebrew, English or any
other language) and get the matching rows back as a JSON array. An LLM you configure
turns the question into SQL; the server runs it read-only and returns the rows as
they are, with the SQL alongside so a wrong answer can be traced.

It works as an MCP server (for any MCP client) and as a command-line tool.

## Setup

Requires Python 3.11 or newer.

```sh
python -m venv .venv
.venv/Scripts/activate        # Windows; on macOS/Linux: source .venv/bin/activate
pip install -e .              # installs the commands below
cp .env.example .env          # then fill in .env
```

### `.env` settings

| Variable | Meaning |
|---|---|
| `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE` | Database connection. Write the password as-is; no quoting or encoding. |
| `PGSSLMODE` | Empty for a local database; `require` for most cloud databases. |
| `LLM_BASE_URL` | Base URL of an OpenAI-compatible model API, usually ending in `/v1`. |
| `LLM_API_KEY` | API key for that URL. |
| `LLM_MODEL` | Model name exactly as the API lists it (default choice: `qwen3.8-27b-fp8`). |
| `MAX_ROWS` | Most rows one query may return (default 500). |
| `QUERY_TIMEOUT_MS` | Longest a query may run, in milliseconds (default 15000). |
| `PGCONNECT_TIMEOUT` | Optional. Seconds to wait for the database to accept a connection (default 10). |

`.env` is git-ignored. Variables already set in the environment override it.

To check the model API before first use (it should list your model):

```sh
curl "$LLM_BASE_URL/models" -H "Authorization: Bearer $LLM_API_KEY"
```

### Use a read-only database user

Every query runs one statement only, inside a read-only transaction, with a timeout
and a row cap. A user that can only read is still the strongest protection:

```sql
CREATE USER mcp_reader WITH PASSWORD 'choose-a-password';
GRANT CONNECT ON DATABASE mydb TO mcp_reader;
GRANT USAGE ON SCHEMA public TO mcp_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mcp_reader;
```

Repeat the last two lines for each other schema the LLM should see.

## Describing your tables

The LLM sees each table's name, columns, types and keys. Where those don't say enough,
add a note in `descriptions.json`; the notes are sent to the LLM under each table. Notes
help most for:

- **Coded values**: `status` holds `A`/`C`. Without a note the model guesses values, and
  the query returns nothing.
- **Cryptic names**: `amt2`, `flg`, `tbl_cst_x`.
- **Similar tables**: `orders` vs `orders_archive`. Say which one to use.
- **Units and conventions**: agorot or shekels, timezone, soft-delete flags.
- **Hebrew words**: the Hebrew term for a table or value, so Hebrew questions map to the
  right names.

Describe only what is ambiguous: every note is sent with every question.

Create a starter file listing every table and column, then fill in what you need:

```sh
postgres-mcp-descriptions-init
```

```json
{
  "customers": {
    "description": "People who bought from us. Hebrew: לקוחות",
    "columns": { "status": "A = active, C = closed (סגור)" }
  },
  "sales.orders": {
    "columns": { "total": "Amount in agorot (1/100 shekel)" }
  }
}
```

- Tables in the `public` schema are keyed by name. Tables in other schemas are keyed as
  `schema.table`.
- `description` and `columns` are optional. Empty strings are ignored, so the starter
  file's blanks can stay.
- A note for a table or column that doesn't exist (a typo, or a renamed table) prints a
  warning and is skipped.
- The CLI reads the file on every run. The MCP server reads it once, so restart your MCP
  client after editing it.

## Command line

```sh
postgres-mcp-ask "customers from Haifa who ordered this month"
```

The rows go to stdout as a JSON array; the generated SQL and any error go to stderr.

## MCP server

Register the server with your MCP client, for example in Claude Code:

```sh
claude mcp add postgres -- "C:/path/to/project/.venv/Scripts/postgres-mcp-server.exe"
```

(On macOS/Linux the path is `.venv/bin/postgres-mcp-server`. `python -m postgres_mcp`
with the venv's Python works too.)

A database URL given as an extra argument overrides the `PG*` settings.

### Tools

- **`ask`** — `question` (string): free text in, rows out. Returns the rows as a JSON
  array, then the SQL that produced them. If Postgres rejects the first SQL, the error
  goes back to the LLM once for a corrected query.
- **`query`** — `sql` (string): runs one SELECT under the same read-only rules and
  returns the rows as a JSON array.

### Resources

- **Table schemas** (`postgres://<host>/<table>/schema`): the column names and types of
  each table in the `public` schema.

### Docker

```sh
docker build -t postgres-mcp-python .
docker run -i --rm --env-file .env postgres-mcp-python
```

`.env` and `descriptions.json` are not copied into the image. Pass settings with
`--env-file`, and mount notes with `-v "$PWD/descriptions.json:/app/descriptions.json:ro"`.
For a database on the host machine, use `PGHOST=host.docker.internal`.

## How values appear in the JSON

`numeric` values are strings, so no digit is lost; dates and times are ISO 8601;
`bytea` is `\x` followed by hex; `json`/`jsonb` columns are nested JSON; UUIDs,
intervals and other types are their text form.

## How it works

| File | Role |
|---|---|
| `src/postgres_mcp/config.py` | Loads `.env`; the limits and LLM settings |
| `src/postgres_mcp/db.py` | Database connections and `run_read_only`, the only path user and LLM SQL runs through |
| `src/postgres_mcp/schema.py` | Describes all tables, columns and keys to the LLM, with the notes (cached) |
| `src/postgres_mcp/descriptions.py` | Loads and checks `descriptions.json` |
| `src/postgres_mcp/init_descriptions.py` | `postgres-mcp-descriptions-init`: writes the starter file |
| `src/postgres_mcp/llm.py` | Calls the model API and extracts the SQL from its reply |
| `src/postgres_mcp/ask.py` | Question → SQL → rows, with one correction retry |
| `src/postgres_mcp/server.py` | MCP server |
| `src/postgres_mcp/cli.py` | Command line |
| `src/postgres_mcp/output.py` | JSON output, error text, and the event loop setup |

With `pip install -e .`, edits in `src/` take effect on the next run, with no build
step. Editing `.env` needs only a restart of the MCP client.

## Development

```sh
pip install -e ".[dev]"
ruff check src tests && ruff format --check src tests
mypy
pytest
```

### Tests

`pytest` runs the whole suite. The tests never read your `.env` or `descriptions.json`,
and never call a real LLM: a local mock server answers in its place.

Tests that need PostgreSQL create a scratch database, seed it, and drop it at the end.
They use, in order of preference:

1. the server in `TEST_DATABASE_URL`, if set. Any database URL on a server where the user
   may run `CREATE DATABASE`, for example
   `TEST_DATABASE_URL=postgresql://postgres:secret@localhost:5432/postgres`;
2. otherwise a throwaway `postgres:17-alpine` container, started and removed through
   Docker.

With neither available, those tests are skipped and the reason is shown.

| Command | What it does |
|---|---|
| `pytest` | All tests except the Docker image test |
| `pytest --run-image` | Also builds the Docker image and checks it end to end |
| `pytest --cov` | Adds a coverage report (the CLI and server subprocesses are included) |
| `pytest tests/test_llm.py -k timeout` | One file, or the tests matching a name |

| Test file | Covers |
|---|---|
| `test_config.py` | `.env` loading (from the project root, environment wins, literal passwords), limits, LLM settings |
| `test_output.py` | JSON conversion of every value type, error text, UTF-8 output, event loop |
| `test_descriptions.py` | Every `descriptions.json` validation message |
| `test_schema_render.py` | Identifier quoting, schema text, notes, unknown-note detection |
| `test_schema_db.py` | Reading tables, views, keys from the catalog; caching; warnings |
| `test_db.py` | Connection settings, and each read-only guard (one statement, read-only transaction, subquery, timeout, row cap) |
| `test_llm.py` | The model request, reply parsing, every error (HTTP, not JSON, no content, unreachable, timeout) |
| `test_ask_unit.py`, `test_ask_db.py` | Question → SQL → rows, and the single correction retry |
| `test_cli.py` | `postgres-mcp-ask` as a program: output streams, exit codes |
| `test_init_descriptions.py` | `postgres-mcp-descriptions-init`: the starter file, never overwriting |
| `test_server.py` | Every MCP handler, and the server over stdio as a client sees it |
| `test_image.py` | The Docker image (with `--run-image`) |
