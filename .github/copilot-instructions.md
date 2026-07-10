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
  The runner fills it at replay so the narrative tracks the data. `data-agg`
  chooses what is computed over `<field>`: `max`/`min` report the highest/lowest
  value and the row it belongs to (that row's first non-numeric column, e.g. a
  facility name), `avg` reports the mean, and `total` reports the sum. Pick the
  aggregation that matches the sentence you want; use one step per sentence.
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

## Free-form: build the report however you like

You do not have to emit contract markup at all. Write the page the way you would
write any page — JavaScript constants, hand-rolled SVG, KPI numbers as plain
`<strong>` text, prose paragraphs. `save_report_definition` fingerprints every
number against what `execute_sql` returned and extracts the structure for you.

**One discipline remains, and it is the whole deal:**

> Every displayed number must be a value `execute_sql` returned.

Put the arithmetic in SQL, not in the page. A gap, a share, a delta, a sort order
— make it a column. A number the page computed in JavaScript has no lineage and
cannot be replayed; the server reports it rather than freezing it silently.

Emitting the v2 contract yourself is still supported and **preferred when it is
cheap**, because a contract artifact skips the confirmation round-trip.

**The round-trip.** A page whose numbers all fingerprint registers in one call. If
extraction has to *infer* anything — derive a query, decide a paragraph should be
regenerated rather than frozen — the first call returns
`status: needs_structure_confirmation` with a `confirmation_token` and a summary
of what it proposes. Show the user the proposal (especially any `derived_queries`
SQL), then call again:

```python
save_report_definition(..., structure_confirmations=[{"token": token, "accept_all": True}])
```

To override individual items instead of accepting everything:

```python
structure_confirmations=[
    {"token": token},
    {"block_id": "b1", "tier": "editorial"},      # freeze this prose, do not regenerate it
    {"derived": "race_gap", "accept": True},
]
```

Nothing is registered until you confirm. `scripts/demo_free_form.py` is a worked
example of the whole flow.

## The v2 contract (charts, tabs, editorial)

The contract above is enough for a table-and-headline report. For a chart-heavy,
tabbed dashboard, use the v2 markers instead. **The two never mix**: any v2 marker
puts the whole artifact on the v2 path. Build them with the `runner/render.py`
builders (`build_island`, `build_value_span_v2`, `build_chart_div`,
`build_bound_table`, `build_reasoning_block`, `build_editorial_block`,
`build_tabs`). See `README.md` for the full grammar and
`scripts/demo_market_story.py` for a worked example.

- **Data islands** carry the results:
  `<script type="application/json" data-result="race_quarters">[…]</script>`.
  Select only JSON-serializable columns. The parity gate compares islands row by
  row and field by field.
- **`data-value`** takes a selector and a filter chain:
  `race_quarters[last].gap_trend | signed | thousands`. Selectors are `.`, `[3]`,
  `[first]`, `[last]`, `[col='val']`. Filters are exactly `thousands`, `signed`,
  `pct(n)`, `pp`, `round(n)`. Add `data-style="sign"` to colour by sign.
- **Charts** (`data-chart='{"type":"line","result":…}'`, types `line` / `bar` /
  `diverging_bar`) and **bound tables**
  (`<table data-result data-columns="field:Header|filter|style:sign">` with an
  empty `<tbody>`) ship as empty markup that `templates/runtime/charts_v1.js`
  fills in the browser.
- **Reasoning v2** states a goal:
  `<p data-reasoning="id" data-goal="…" data-inputs="a, b[col='v']" data-max-sentences="3"></p>`.
- **Editorial blocks** (`data-editorial`, `data-authored-as-of`, optional
  `data-watch="ref OP number"`) replay verbatim and are hashed. A watch that fires
  at replay prepends a staleness banner.
- Set `layout: "tabbed-dashboard"` and `theme: "market-story-v1"` on
  `final_artifact`, declare tabs with `<nav data-tabs='[…]'>`, and mark content
  with `data-section`. **A tabbed layout renders HTML only**; `formats` is forced
  to `["html"]`.
- The compiler **rejects** `{{ }}` / `{% %}` in your artifact and non-whitelisted
  filters, and **warns** about a literal `Q1'25` in a heading or `.kpi-label` —
  bind it to a result instead, or it will lie at the next replay.

## Repository conventions

- The core path is **offline and LLM-free**: no GCP, no network calls, no LLM
  calls. Keep it that way. LLM-backed reasoning is opt-in behind the
  `ReasoningEngine` protocol (`server/reasoning.py`) and off by default.
- **Computation stays in SQL.** The chart runtime draws and formats; it never
  derives. If a chart needs a gap, a share, a delta, a display string, or a
  particular row order, add a column or an `ORDER BY` to the query — and give
  every value-ordered `ORDER BY` a tiebreaker, since ties come back in an
  arbitrary order and the parity gate compares islands positionally.
- Data is synthetic, seeded with a fixed random seed and anchored at **2025-06-30**
  so parity results are stable. Run `python data/seed.py` (idempotent) before use.
- The compiler (`Distiller`) and reasoning engine (`ReasoningEngine`) are
  deterministic implementations behind protocols. Prefer swapping an implementation
  behind the protocol over editing call sites.
- Storage is split: `data/poc.duckdb` (the warehouse, opened **read-only** by
  everyone, so processes coexist) and `data/poc_meta.sqlite` (the lineage log and
  definition registry, SQLite in WAL mode). `regenerate.py` runs fine while the
  server is up. Never move a writable table into the DuckDB file -- a read-write
  open takes an exclusive lock that shuts out every other process.
- `python data/seed.py` is the exception: it opens the warehouse read-write, so
  nothing else may be attached while it runs.
- Run the tests with `pytest -q` before committing changes to server or runner code.
