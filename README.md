# PostgreSQL MCP server

Ask your PostgreSQL database a question in plain language (Hebrew, English or any
other language) and get the matching rows back as a JSON array. An LLM you configure
turns the question into SQL; the server runs it read-only and returns the rows as
they are, with the SQL alongside so a wrong answer can be traced.

It works as an MCP server (for any MCP client) and as a command-line tool.

## Setup

Requires Python 3.11 or newer.

```sh
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env          # then fill in .env
```

```powershell
# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
Copy-Item .env.example .env   # then fill in .env
```

If PowerShell refuses to run `Activate.ps1`, allow local scripts once with
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

`pip install -e .` installs three commands into the virtual environment:

| Command | What it does |
|---|---|
| `postgres-mcp-server` | Starts the MCP server (also: `python -m postgres_mcp`) |
| `postgres-mcp-ask` | Asks one question from the command line |
| `postgres-mcp-descriptions-init` | Writes a starter `descriptions.json` |

### `.env` settings

| Variable | Meaning |
|---|---|
| `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE` | Database connection. Write the password as-is; no quoting or encoding. |
| `PGSSLMODE` | Empty for a local database; `require` for most cloud databases. |
| `PGCONNECT_TIMEOUT` | Optional. Seconds to wait for the database to accept a connection (default 10). |
| `LLM_BASE_URL` | Base URL of an OpenAI-compatible model API, usually ending in `/v1`. |
| `LLM_API_KEY` | API key for that URL. |
| `LLM_MODEL` | Model name exactly as the API lists it (default choice: `qwen3.8-27b-fp8`). |
| `MAX_ROWS` | Most rows one query may return (default 500). |
| `QUERY_TIMEOUT_MS` | Longest a query may run, in milliseconds (default 15000). |

- `.env` is read from the project folder, wherever the program is started from.
- Variables already set in the environment override it.
- `.env` is git-ignored.
- Without the `LLM_*` settings, the `query` tool and the schema resources still work;
  only `ask` reports what is missing.

To check the model API before first use (it should list your model):

```powershell
# PowerShell
Invoke-RestMethod "$env:LLM_BASE_URL/models" -Headers @{ Authorization = "Bearer $env:LLM_API_KEY" }
```

```sh
# macOS / Linux
curl "$LLM_BASE_URL/models" -H "Authorization: Bearer $LLM_API_KEY"
```

These read the variables from your shell, so set them there first, or paste the values
in.

### Use a read-only database user

Every query is confined in four ways:

1. **One statement only:** Postgres refuses a second one (this is what stops
   `COMMIT; DELETE ...`).
2. **Read only:** it runs in a read-only transaction that is always rolled back.
3. **Row queries only:** it is wrapped as a subquery, so only a query that returns rows
   is accepted.
4. **Limits:** a time limit (`QUERY_TIMEOUT_MS`) and a row limit (`MAX_ROWS`).

A database user that can only read is still the strongest protection:

```sql
CREATE USER mcp_reader WITH PASSWORD 'choose-a-password';
GRANT CONNECT ON DATABASE mydb TO mcp_reader;
GRANT USAGE ON SCHEMA public TO mcp_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mcp_reader;
-- Also tables created later:
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO mcp_reader;
```

Repeat the last three lines for each other schema the LLM should see.

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

It never overwrites an existing `descriptions.json`.

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
- A malformed file stops the question with an error naming the file and the bad entry.
- The CLI reads the file on every run. The MCP server reads it once, so restart your MCP
  client after editing it.

## Command line

```sh
postgres-mcp-ask "customers from Haifa who ordered this month"
```

The rows go to stdout as a JSON array; the generated SQL and any error go to stderr. The
output is UTF-8, so it can be piped or saved:

```sh
postgres-mcp-ask "לקוחות מחיפה" > rows.json
```

The exit code is 0 on success and 1 on any error.

## MCP server

Two separate programs are involved:

- **The model that writes the SQL** is the one set in `.env` (`LLM_BASE_URL`,
  `LLM_MODEL`). The server calls it for every `ask`.
- **The MCP client** is the app that connects to this server and calls its tools. It can
  be any MCP client; the server does not depend on which one.

The server talks over stdio. Register it with your MCP client by giving the client this
command:

```sh
/path/to/postgres-mcp/.venv/bin/postgres-mcp-server
```

Many clients take it in this JSON form:

```json
{
  "mcpServers": {
    "postgres": {
      "command": "/path/to/postgres-mcp/.venv/bin/postgres-mcp-server"
    }
  }
}
```

- **Command path:** on Windows it is `.venv\Scripts\postgres-mcp-server.exe`. Running
  `python -m postgres_mcp` with the virtual environment's Python works too.
- **Settings:** the server reads `.env` from the project folder itself, so the client
  needs no environment settings.
- **Database URL:** given as an extra argument (`args` in JSON), it overrides the `PG*`
  settings.
- **Without an MCP client:** use the [command line](#command-line) instead.

### Tools

- **`ask`** — `question` (string): free text in, rows out. Returns the rows as a JSON
  array, then the SQL that produced them. If Postgres rejects the first SQL, the error
  goes back to the LLM once for a corrected query.
- **`query`** — `sql` (string): runs one SELECT under the same read-only rules and
  returns the rows as a JSON array.

- **Read-only marking:** both tools are marked read-only, so clients that support tool
  annotations can run them without asking each time. `ask` is also marked as reaching
  outside the database, because it calls the LLM.
- **Errors:** a failed query or LLM call is returned as a tool error with the reason, for
  example `syntax error at or near "DELETE"` or `LLM request failed (401): ...`. A blank
  argument gives `sql must be a non-empty string` (or `question ...`). A missing or
  non-string argument is rejected against the tool's input schema.
- **Instructions:** on connecting, the server tells the client's model when to use each
  tool, and that the table schemas are available as resources.

### Resources

- **Table schemas** (`postgres://<host>:<port>/<table>/schema`): the column names and
  types of each table and view in the `public` schema, as JSON. The URIs never contain
  the user or password.
- **Listing:** every table is listed as its own resource, sorted by name. The same URI
  is also offered as a resource template (`.../{table}/schema`), for clients that fill in
  the table name themselves.
- **Errors:** a table that doesn't exist returns `Table "<name>" not found`. A URI of any
  other form returns `Unknown resource: <uri>`.

### Docker

```sh
docker build -t postgres-mcp-python .
docker run -i --rm --env-file .env postgres-mcp-python
```

- **Settings:** `.env` and `descriptions.json` are not copied into the image. Pass
  settings with `--env-file`.
- **Notes:** mount `descriptions.json` with
  `-v "$PWD/descriptions.json:/app/descriptions.json:ro"`.
- **Database on this machine:** set `PGHOST=host.docker.internal`, because inside the
  container `localhost` is the container itself.
- **User:** the container runs as a non-root user.

## How values appear in the JSON

| PostgreSQL type | JSON |
|---|---|
| `integer`, `bigint` | number |
| `real`, `double precision` | number; `NaN` and `Infinity` become `null` |
| `numeric` | string, so no digit is lost: `"1234567890.12"` |
| `boolean` | `true` / `false` |
| `text`, `varchar`, `char` | string |
| `date`, `time`, `timestamp`, `timestamptz` | ISO 8601 string: `"2026-11-01"`, `"2026-11-01T10:00:00+00:00"` |
| `interval` | Python's form: `"1 day, 2:00:00"` |
| `json`, `jsonb` | nested JSON |
| arrays | JSON arrays |
| `bytea` | `"\\xdeadbeef"` (`\x` followed by hex) |
| `uuid`, `inet`, ranges and other types | their text form: `"[1, 5)"` |

## Troubleshooting

| Message | Cause and fix |
|---|---|
| `Missing database settings: ...` | Fill in the named `PG*` settings in `.env`, or pass a database URL. |
| `connection timeout expired` | Nothing answered at `PGHOST`:`PGPORT`. Check that the database is running and the host and port are right. |
| `password authentication failed` | Wrong `PGUSER` or `PGPASSWORD`. |
| `Missing LLM settings: ...` | Fill in the named `LLM_*` settings in `.env`. |
| `Could not reach the LLM at ...` | Wrong `LLM_BASE_URL`, or the model host is down. The message ends with the reason. |
| `LLM request failed (401): ...` | Wrong `LLM_API_KEY`. A 404 usually means a wrong `LLM_BASE_URL` or `LLM_MODEL`. |
| `... must be a positive whole number` | Fix `MAX_ROWS` or `QUERY_TIMEOUT_MS`. |
| `canceling statement due to statement timeout` | The query ran longer than `QUERY_TIMEOUT_MS`. |
| New tables or columns are not used | The MCP server reads the schema once; restart your MCP client. |
| `Unknown resource: ...` | The URI is not `postgres://<host>:<port>/<table>/schema`, or its host and port differ from the server's. Take URIs from the resource list. |
| VS Code: `Package ... is not installed` | Select `.venv` with **Python: Select Interpreter**. |

## Known limitations

- **Semicolon before a comment:** a query ending in a semicolon followed by a comment
  (`SELECT ...; -- note`) is rejected by Postgres. In `ask`, the error goes back to the
  LLM, which returns the query without it, at the cost of one extra LLM call.
- **Schema cache:** the MCP server reads the schema and `descriptions.json` once; restart
  it after changing either.

## How it works

The path of one question, from the client to the rows:

```text
 MCP client ──ask──►┐
                    ├─► 1. settings ─► 2. schema ─► 3. prompt ─► 4. LLM writes SQL
 postgres-mcp-ask ─►┘                                                 │
                                                                      ▼
            7. rows + SQL back ◄─ 6. one retry if rejected ◄─ 5. run read-only
```

1. **Settings** (`config.py`, `db.py`): at startup, `.env` is loaded from the project
   folder and the database settings are checked. A missing setting stops the program
   with a message naming it.
2. **Schema** (`schema.py`, `descriptions.py`): two catalog queries read every table and
   view in every user schema, with their columns, types, primary keys and foreign keys.
   The notes from `descriptions.json` are added under their tables. The result is compact
   text, one line per table. The MCP server builds it once and keeps it until it
   restarts; the command line builds it on every run.
3. **Prompt** (`llm.py`): the model receives fixed rules (one SELECT only, use only the
   listed names, return only SQL), then the schema text and the question.
4. **LLM** (`llm.py`): the request goes to `LLM_BASE_URL/chat/completions` with
   temperature 0, so the same question gives the same SQL. The model gets 120 seconds to
   answer. Reasoning (`<think>...</think>`) and code fences are removed from the reply,
   leaving the SQL.
5. **Run read-only** (`db.py`): the SQL runs on a new database connection, under the
   four protections in [Use a read-only database user](#use-a-read-only-database-user).
   The transaction is rolled back and the connection closed after every query, whether
   it succeeded or not.
6. **One retry** (`ask.py`): if Postgres rejects the SQL (for example a wrong column
   name), its error message goes back to the model, which writes a corrected query. That
   query runs the same way. If it fails too, the error is returned; there is no third
   try.
7. **Rows back** (`output.py`): the rows are turned into a JSON array (see
   [How values appear in the JSON](#how-values-appear-in-the-json)) and returned with
   the SQL that produced them:
   - **MCP:** two text items, the rows and then `SQL: ...`. An error comes back as a tool
     error with the reason.
   - **Command line:** the rows on stdout, the SQL on stderr. An error goes to stderr
     with exit code 1.

The other entry points use parts of this path:

- **`query` tool:** skips steps 2–4 and 6. The SQL it is given runs directly in step 5.
- **Schema resources:** read the table list and each table's columns straight from the
  catalog. No model is involved.
- **`postgres-mcp-descriptions-init`:** runs step 2's catalog queries and writes the
  table and column names into a starter `descriptions.json`.

## Project layout

| Path | Role |
|---|---|
| `src/postgres_mcp/config.py` | Loads `.env`; the limits and LLM settings |
| `src/postgres_mcp/db.py` | Database connections and `run_read_only`, the only path user and LLM SQL runs through |
| `src/postgres_mcp/schema.py` | Describes all tables, columns and keys to the LLM, with the notes (cached) |
| `src/postgres_mcp/descriptions.py` | Loads and checks `descriptions.json` |
| `src/postgres_mcp/init_descriptions.py` | `postgres-mcp-descriptions-init`: writes the starter file |
| `src/postgres_mcp/llm.py` | Calls the model API and extracts the SQL from its reply |
| `src/postgres_mcp/ask.py` | Question → SQL → rows, with one correction retry |
| `src/postgres_mcp/server.py` | MCP server: the tools and resources, built on the MCP SDK's `MCPServer` (formerly FastMCP) |
| `src/postgres_mcp/__main__.py` | `python -m postgres_mcp` starts the MCP server |
| `src/postgres_mcp/cli.py` | Command line |
| `src/postgres_mcp/output.py` | JSON output, error text, and the event loop setup |
| `tests/` | The test suite (see [Tests](#tests)) |
| `pyproject.toml` | Package, dependencies, commands, and ruff/mypy/pytest settings |
| `Dockerfile`, `.dockerignore` | The container image |
| `.env.example` | Template for `.env` |

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
| `test_server.py` | Tools, resources, annotations and errors through an in-process MCP client, and the server over stdio as a real client sees it |
| `test_image.py` | The Docker image (with `--run-image`) |
