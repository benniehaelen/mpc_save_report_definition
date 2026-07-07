# Viewing the DuckDB data

The POC database lives at `data/poc.duckdb`. Because DuckDB takes an **exclusive
lock** in read-write mode, only one process can open the file at a time, so
**stop the MCP server before browsing the data** (otherwise the viewer reports
that the file is locked).

## Option 1: Harlequin (terminal DuckDB browser)

Harlequin is already included in the `dev` extra (`pip install -e ".[dev]"`).

```bash
# 1. Make sure no hin-poc server holds the lock
powershell -File scripts/servers.ps1 -Kill

# 2. Open the database (read-only is safest for browsing)
.venv/Scripts/harlequin -r data/poc.duckdb
```

- `-r` opens **read-only**, so you can't accidentally lock out the server or
  mutate data. Drop it if you actually want to run writes.
- The left panel lists tables: `admissions`, `daily_census`, `facilities`,
  `metrics`, `value_sets`, `dimension_value_sets`, `report_definitions`,
  `tool_call_log`.
- Type SQL in the editor and press **Ctrl+Enter** to run it.
- Quit with **Ctrl+Q**.

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
# Stop the server first so the DB lock is free
powershell -File scripts/servers.ps1 -Kill

.venv/Scripts/python.exe -c "import json; from server import registry; from server.db import get_connection; print(json.dumps(registry.get(get_connection(read_only=True), 'division_admissions_and_census'), indent=2, default=str))"
```

`registry.get(con, report_id, version=None)` defaults to the latest version;
pass a version number as the third argument for a specific one.

Find the available `report_id`s with:

```bash
.venv/Scripts/python.exe runner/regenerate.py --list
```

Or query the raw column directly in Harlequin (shown as one long JSON string):

```sql
SELECT definition_json
FROM report_definitions
WHERE report_id = 'division_admissions_and_census'
ORDER BY definition_version DESC
LIMIT 1;
```

> Note: `report_definitions` starts empty. Save a report first (for example
> `python scripts/demo_session.py`) before there is any JSON to fetch.

## Reminder

Close the viewer (Harlequin or the Python process) **before reconnecting the
`hin-poc` server** — only one process can hold the DuckDB file at a time. If a
viewer is still open, the server will fail to start with a "database is locked"
message.
