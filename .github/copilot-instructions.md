# Copilot instructions: building re-generatable reports with the `hin-poc` MCP tools

This repository is a proof of concept for **re-generatable reports**. A report is
not saved as a frozen document — it is saved as a *definition* that can be
replayed as of any date. You (the Copilot) drive four MCP tools to build a report
once; the runner then replays it automatically.

When the `hin-poc` MCP server is connected, follow the workflow below whenever the
user asks you to build, save, or regenerate a report.

## The build loop

1. **Reuse one `conversation_id`** string for every tool call in the session. It
   is the session key that ties your queries together into lineage.
2. Call **`nl_query(conversation_id, question)`** with the user's question. Read
   `suggested_sql` and the returned table and column lists. It executes nothing.
3. Refine the SQL, then **`dry_run_sql(conversation_id, sql)`**. Fix any error and
   dry-run again until it validates. Dry runs are not recorded as lineage.
4. **`execute_sql(conversation_id, sql, result_name)`** with a clear snake_case
   `result_name` (for example `admissions_by_division`). Repeat for each result
   the report needs. Do not reference throwaway queries in the report — unreferenced
   results drop out of the definition automatically.
5. Build the report as an **HTML body fragment** following the artifact contract
   below.
6. Call **`save_report_definition(conversation_id, report_name, transcript,
   final_artifact, temporal_confirmations=None)`**, where:
   - `transcript` is `[{role, content}]`.
   - `final_artifact = {format: "html", title, content, formats: ["html", "md"]}`.
7. Report back `status`, `parity.passed`, `report_id`, and any `warnings` or
   `unreplayable_sections`.

If parity fails, the response names the first differing table or value. The usual
cause is cell text that does not match the query output — see the contract.

## How to build the HTML artifact (the contract)

The compiler recovers a report's lineage from the HTML you submit. Every
replayable section must carry data attributes tying rendered content back to named
query results.

- **Every replayable table** must be `<table data-result="<result_name>">`, where
  `<result_name>` matches the name you passed to `execute_sql`. Give it a `<thead>`
  of column names and a `<tbody>` of the data rows. The compiler replaces the
  `<tbody>` with a loop over the named result at replay time.
- **Every headline number** must be wrapped in an element carrying
  `data-value="<result_name>.<field>"`, for example
  `<span data-value="overall_occupancy.occupancy_rate">0.7314</span>`. The value is
  pulled from the first row of that result.
- **Each narrative sentence** you want recomputed over fresh results is an *empty*
  element with `data-reasoning="<step_id>"`, `data-over="<result_name>.<field>"`,
  and optional `data-agg="max|min|avg|total"` (default `max`), for example
  `<p data-reasoning="occ_summary" data-over="census_by_facility.occupancy_rate" data-agg="max"></p>`.
  The runner fills it at replay so the narrative tracks the data.
- **Metric bindings are inferred** by matching table headers and `data-value`
  fields to metric ids and dimension ValueSets in the knowledge graph. No extra
  markup is needed; unknown fields are left unbound.
- **Build cell and headline text directly from the values `execute_sql` returned.**
  The parity gate compares extracted numbers, not markup, so hand-formatted or
  rounded values that differ from the query output will fail parity. (Narrative
  prose is not compared, since it carries no `data-value`.)
- Request output formats with `final_artifact["formats"]`, for example
  `["html", "md"]`. HTML is always included.
- Submit `content` as a **body fragment** (heading, tables, headline spans,
  reasoning placeholders) — no `<html>`/`<head>`/`<body>` shell. The base page
  shell is added at render time.

Anything the compiler cannot template (a table with no matching query, a
`data-value` referencing an unknown result) is reported in `unreplayable_sections`
rather than silently kept.

Convenience builders live in `runner/render.py` (`build_table_html`,
`build_value_span`) if you want to construct a parity-safe artifact directly.

## Regenerating a saved report

```
python runner/regenerate.py --report-id <id> [--version N] [--as-of 2025-06-15] [--out reports/] [--formats html,md]
python runner/regenerate.py --list
```

The runner binds `__REPORT_DATE__` to `--as-of` (default: the anchor date
2025-06-30), executes the named queries, reasons over the fresh results, renders
each requested format, and writes `reports/<report_id>_v<version>_<as_of>.<ext>`.

## Repository conventions

- The core path is **offline and LLM-free**: no GCP, no network calls, no LLM
  calls. Keep it that way. LLM-backed reasoning is opt-in behind the
  `ReasoningEngine` protocol (`server/reasoning.py`) and off by default.
- Data is synthetic, seeded with a fixed random seed and anchored at **2025-06-30**
  so parity results are stable. Run `python data/seed.py` (idempotent) before use.
- The compiler (`Distiller`) and reasoning engine (`ReasoningEngine`) are
  deterministic implementations behind protocols. Prefer swapping an implementation
  behind the protocol over editing call sites.
- The server holds an exclusive read-write lock on the DuckDB file. DuckDB will
  not let a second process open the same file while it is held read-write, so
  **stop the MCP server before running `regenerate.py`** (it now reports a clear
  "database is locked" message if you forget).
- Run the tests with `pytest -q` before committing changes to server or runner code.
