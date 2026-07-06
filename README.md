# Re-generatable Reports POC

A local, cloud-free proof of concept of the re-generatable reports design: an MCP
server with NL-to-SQL support tools, a local DuckDB database with synthetic
healthcare-flavored data, a `save_report_definition` tool with a working parity
gate, and a CLI runner that replays saved reports as of any date. Everything runs
on this machine. No GCP, no network calls, no LLM calls in the core path.

## The idea

A report is not saved as a frozen document. It is saved as a *definition*: the
named SQL query set that produced it, plus a rendering template. Date literals in
the SQL are rewritten relative to a report-date token, so the same definition can
be replayed as of any date and produce the same structure with different data.

Before a definition is registered, a **parity gate** proves it reproduces the
original artifact exactly (comparing extracted data values, not markup). If it
cannot, the report is not saved.

## Setup

```
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux
pip install -e .
python data/seed.py               # creates data/poc.duckdb (idempotent)
```

The synthetic data spans 120 days ending on the fixed anchor date **2025-06-30**,
with a fixed random seed so parity results are stable.

## Running the MCP server

```
python server/main.py
```

This starts the `hin-poc` FastMCP server over stdio, exposing four tools. To
connect it to Claude Code, register it in your MCP config, for example:

```json
{
  "mcpServers": {
    "hin-poc": {
      "command": "C:\\src\\mpc_save_report_definition\\.venv\\Scripts\\python.exe",
      "args": ["C:\\src\\mpc_save_report_definition\\server\\main.py"]
    }
  }
}
```

### The four tools

Every tool takes `conversation_id` as its first argument (an opaque session key).

- **nl_query(conversation_id, question)** maps a natural language question to a
  matched intent, the relevant tables with their columns, and a suggested SQL
  template for the last 30 days ending at the anchor. Executes nothing.
- **dry_run_sql(conversation_id, sql)** validates a single SELECT and returns its
  result columns. Not recorded as lineage.
- **execute_sql(conversation_id, sql, result_name)** runs a validated SELECT
  (capped at 500 rows), assigns it `result_name`, logs the call, and returns the
  rows. The `result_name` is what makes named results work later.
- **save_report_definition(conversation_id, report_name, transcript,
  final_artifact, temporal_confirmations=None)** distills a named query set from
  the logged calls, runs the parity gate against the final artifact, and
  registers the definition on pass.

## IMPORTANT: how the client must build the HTML artifact

The compiler recovers a report's lineage from the HTML you submit. For a section
to be replayable, the artifact must carry data attributes that tie rendered
content back to named query results:

- **Every table** that should be replayed must be
  `<table data-result="<result_name>">` where `<result_name>` matches the name
  you passed to `execute_sql`. The table needs a `<tbody>` holding the data rows;
  the compiler replaces that body with a loop over the named result.
- **Every headline number** must be wrapped in an element carrying
  `data-value="<result_name>.<field>"`, for example
  `<span data-value="overall_occupancy.occupancy_rate">0.7314</span>`. The value
  is pulled from the first row of that result.
- **Build cell and headline text directly from the values `execute_sql`
  returned.** The parity gate compares extracted numbers, so hand-formatted or
  rounded values that differ from the query output will fail parity.
- Submit the artifact `content` as a **body fragment** (heading, tables, headline
  spans). The base page shell is added at render time.

Anything the compiler cannot template (a table with no matching query, a
`data-value` referencing an unknown result) is reported in
`unreplayable_sections` rather than silently kept. This is the POC's stand-in for
the strategic render_report tool.

Convenience builders live in `runner/render.py` (`build_table_html`,
`build_value_span`) if you want to construct a parity-safe artifact directly.

## Regenerating a report

```
python runner/regenerate.py --report-id <id> [--version N] [--as-of 2025-06-15] [--out reports/]
python runner/regenerate.py --list
```

Binds `__REPORT_DATE__` to `--as-of` (default: the anchor date), executes the
named queries, renders the template, and writes
`reports/<report_id>_v<version>_<as_of>.html`. Prints one line per query.

Note: the server holds the single read-write lock on the DuckDB file, so the
runner opens the database read-only. It can run while the server is up.

## How it fits together

```
nl_query / dry_run_sql / execute_sql   ->  tool_call_log (lineage)
save_report_definition
   -> compiler.distill      match artifact result names to logged queries
   -> temporal.reparameterize   rewrite date literals to __REPORT_DATE__ offsets
   -> parity.check          re-execute + render + compare extracted values
   -> registry.register     store the definition document as JSON
runner/regenerate.py        bind __REPORT_DATE__ to --as-of, replay, render
```

The compiler is a deterministic heuristic behind a `Distiller` protocol, so an
LLM-backed distiller can replace it without touching the rest of the system.

## Temporal re-parameterization

The definition stores SQL with the token `__REPORT_DATE__`. A literal such as
`DATE '2025-06-01'` with anchor `2025-06-30` becomes
`__REPORT_DATE__ - INTERVAL 29 DAY`. The transformation is conservative: a
literal more than ~366 days from the anchor, or one the caller marks `fixed` via
`temporal_confirmations`, is left as-is and a warning is recorded.

## Tests

```
pytest -q
```

- `tests/test_temporal.py` covers literal detection, relative rewriting, fixed
  passthrough, and the conservative fallback.
- `tests/test_parity.py` covers a matching artifact passing, an edited cell
  failing and being named, and markup-only differences passing.
- `tests/test_end_to_end.py` runs a scripted session in-process, saves, and
  regenerates as of a different date, asserting the structure holds and the
  values change.

## Demo walkthrough (acceptance test)

With the server connected in Claude Code:

1. `python data/seed.py`
2. Start the server; confirm the four tools are visible.
3. Ask for admissions by division for the last 30 days. Use `nl_query`, refine
   the suggested SQL, `dry_run_sql` it, then `execute_sql` as
   `admissions_by_division`.
4. Execute a second query as `census_by_facility` (average census and occupancy).
5. Run one throwaway query first as `scratch` and never reference it, to prove
   dead ends drop out.
6. Build an HTML report: a heading, the two tables with `data-result` attributes,
   and one headline occupancy number with a `data-value` attribute.
7. Call `save_report_definition` with the transcript and the artifact. Confirm:
   status registered, parity passed, `scratch` absent from the definition, and
   the stored SQL contains `__REPORT_DATE__` instead of literal dates.
8. `python runner/regenerate.py --report-id <id> --as-of 2025-05-15` and open the
   output: same structure, different numbers.
9. `pytest -q`, all green.

## Project layout

```
data/seed.py           creates and populates poc.duckdb
server/main.py         FastMCP entry point
server/tools.py        the four MCP tools
server/intent_catalog.py   keyword intents for nl_query
server/call_log.py     tool-call log keyed by conversation_id
server/compiler.py     distillation: session record -> definition
server/temporal.py     date literal detection and re-parameterization
server/parity.py       the parity gate
server/registry.py     definition registry (DuckDB tables)
server/db.py           shared DB path, connection, and constants
runner/regenerate.py   CLI replay runner
runner/render.py       Jinja2 rendering shared with the parity gate
templates/report_base.html.j2   base report template
tests/                 temporal, parity, end-to-end
```

Owner identity is out of scope for this POC.
