# Viewing the data

The analytic warehouse lives at `data/poc.duckdb`, and the platform metadata --
`tool_call_log` and `report_definitions` -- lives at `data/poc_meta.sqlite`.

Browsing is safe while the MCP server is running, **as long as you open the
warehouse read-only** (`-r`). Read-only DuckDB connections take a shared lock and
coexist happily. A read-write open takes an exclusive lock that shuts out the
server and everything else, so don't drop the `-r`.

## Option 1: Harlequin (terminal DuckDB browser)

Harlequin is already included in the `dev` extra (`pip install -e ".[dev]"`).

```bash
.venv/Scripts/harlequin -r data/poc.duckdb
```

- `-r` opens **read-only**. Keep it: without it, DuckDB takes an exclusive lock
  and the running server loses access to the warehouse.
- The left panel lists tables: `admissions`, `daily_census`, `facilities`,
  `marketshare_volume`, `metrics`, `value_sets`, `dimension_value_sets`.
- Type SQL in the editor and press **Ctrl+Enter** to run it.
- Quit with **Ctrl+Q**.

`report_definitions` and `tool_call_log` are **not** in this file. Browse them
with any SQLite client:

```bash
sqlite3 data/poc_meta.sqlite "SELECT report_id, definition_version FROM report_definitions;"
```

Example queries:

```sql
SELECT * FROM report_definitions;      -- registered report definitions
SELECT * FROM admissions LIMIT 20;
SELECT * FROM facilities;
```

## Option 2: Quick one-liner (no TUI)

```bash
.venv/Scripts/python.exe -c "import duckdb; print(duckdb.connect('data/poc.duckdb', read_only=True).sql('SELECT * FROM facilities'))"
```

Swap the query for whatever you need.

## Inspecting a full report definition (JSON)

Each saved report is stored as a JSON document in the `definition_json` column
of the `report_definitions` table. The cleanest way to pull it out
pretty-printed is via the `registry.get()` helper, which parses the JSON for
you:

```bash
.venv/Scripts/python.exe -c "import json; from server import registry; from server.db import get_meta_connection; print(json.dumps(registry.get(get_meta_connection(), 'division_admissions_and_census'), indent=2, default=str))"
```

`registry.get(con, report_id, version=None)` defaults to the latest version;
pass a version number as the third argument for a specific one.

Find the available `report_id`s with:

```bash
.venv/Scripts/python.exe runner/regenerate.py --list
```

Or query the raw column directly against the metadata store (one long JSON
string):

```sql
-- sqlite3 data/poc_meta.sqlite
SELECT definition_json
FROM report_definitions
WHERE report_id = 'division_admissions_and_census'
ORDER BY definition_version DESC
LIMIT 1;
```

> Note: `report_definitions` starts empty. Save a report first (for example
> `python scripts/demo_session.py`) before there is any JSON to fetch.

## Reminder

You no longer need to stop the server to browse the data. The one thing that
still demands exclusive access is `python data/seed.py`, which rebuilds the
warehouse read-write — close viewers and stop the server before reseeding.
