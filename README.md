# Re-generatable Reports POC

A local, cloud-free proof of concept of the re-generatable reports design: an MCP
server with NL-to-SQL support tools, a local DuckDB database with synthetic
healthcare-flavored data, a `save_report_definition` tool with a working parity
gate, and a CLI runner that replays saved reports as of any date. Everything runs
on this machine. No GCP, no network calls, no LLM calls in the core path.

## The idea

A report is not saved as a frozen document. It is saved as a *definition* with
four parts, mirroring the design in Figure 1:

- **Parameterized SQL**: the named query set, with date literals rewritten
  relative to a report-date token so the report can be replayed as of any date.
- **Metric bindings**: result fields bound to governed metrics and ValueSets in
  the knowledge graph, validated at save time and again at replay.
- **Reasoning steps**: steps the runner replays over freshly executed results to
  produce narrative that changes with the data.
- **Rendering spec**: the template plus the output formats (HTML, Markdown).

Before a definition is registered, a **parity gate** proves it reproduces the
original artifact exactly (comparing extracted data values, not markup). If it
cannot, the report is not saved.

### How this maps to Figure 1 (cloud-free)

| Design element        | POC stand-in                                   |
|-----------------------|------------------------------------------------|
| BigQuery              | local DuckDB (`data/poc.duckdb`)               |
| Knowledge graph       | `metrics` / `value_sets` catalog tables        |
| Report registry       | `report_definitions` table (`server/registry`) |
| Generic runner (Cloud Run) | `runner/regenerate.py`, direct execution  |
| LLM service           | `server/reasoning.py` (deterministic engine behind a protocol) |
| Render and deliver to GCS | multi-format render to `reports/`          |
| Observability / OTel spans | local spans in `logs/spans.jsonl`         |
| Cloud Scheduler       | invoke the runner CLI per report               |

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
- **Each narrative sentence** you want the runner to recompute over fresh results
  is an empty element with `data-reasoning="<step_id>"`,
  `data-over="<result_name>.<field>"`, and optional `data-agg="max|min|avg|total"`
  (default `max`), for example
  `<p data-reasoning="occ_summary" data-over="census_by_facility.occupancy_rate" data-agg="max"></p>`.
  The runner fills it at replay, so the narrative tracks the data.
- **Metric bindings are inferred** by matching table headers and `data-value`
  fields to metric ids and dimension ValueSets in the knowledge graph. No extra
  markup is needed; unknown fields are simply left unbound.
- **Build cell and headline text directly from the values `execute_sql`
  returned.** The parity gate compares extracted numbers, so hand-formatted or
  rounded values that differ from the query output will fail parity. (Narrative
  prose is not compared, since it carries no `data-value`.)
- Request output formats with `final_artifact["formats"]`, for example
  `["html", "md"]`. HTML is always included.
- Submit the artifact `content` as a **body fragment** (heading, tables, headline
  spans, reasoning placeholders). The base page shell is added at render time.

Anything the compiler cannot template (a table with no matching query, a
`data-value` referencing an unknown result) is reported in
`unreplayable_sections` rather than silently kept. This is the POC's stand-in for
the strategic render_report tool.

Convenience builders live in `runner/render.py` (`build_table_html`,
`build_value_span`) if you want to construct a parity-safe artifact directly.

## Regenerating a report

```
python runner/regenerate.py --report-id <id> [--version N] [--as-of 2025-06-15] [--out reports/] [--formats html,md]
python runner/regenerate.py --list
```

Fetches the definition, validates its bindings against the knowledge graph, binds
`__REPORT_DATE__` to `--as-of` (default: the anchor date), executes the named
queries, runs the reasoning steps over the fresh results, renders every requested
format, and writes `reports/<report_id>_v<version>_<as_of>.<ext>`. Each step is
recorded as a local observability span in `logs/spans.jsonl`.

Note: the server holds the single read-write lock on the DuckDB file, so the
runner opens the database read-only. It can run while the server is up.

## How it fits together

```
nl_query / dry_run_sql / execute_sql   ->  tool_call_log (lineage)
save_report_definition
   -> compiler.distill        parameterized_sql, metric_bindings,
                              reasoning_steps, rendering_spec
   -> temporal.reparameterize rewrite date literals to __REPORT_DATE__ offsets
   -> knowledge_graph         validate metric and ValueSet bindings
   -> parity.check            re-execute + reason + render + compare values
   -> registry.register       store the definition document as JSON
runner/regenerate.py          validate bindings, bind __REPORT_DATE__ to --as-of,
                              execute, reason over fresh results, render, record spans
```

The compiler is a deterministic heuristic behind a `Distiller` protocol, and the
reasoning engine is a deterministic implementation behind a `ReasoningEngine`
protocol. Either can be swapped for an LLM-backed version without touching the
rest of the system, and the default core path stays LLM-free and offline.

### Optional LLM-backed reasoning

An `AnthropicReasoningEngine` (the design's LLM service) is wired behind the same
`ReasoningEngine` protocol. It is opt-in and off by default:

```
pip install -e ".[llm]"          # installs the anthropic SDK
set POC_REASONING=anthropic       # Windows; use export on macOS/Linux
# authenticate the SDK (ANTHROPIC_API_KEY or `ant auth login`)
python runner/regenerate.py --report-id <id> --as-of 2025-05-15
```

It uses `claude-opus-4-8` (override with `POC_REASONING_MODEL`) with adaptive
thinking to write one grounded sentence per reasoning step over the fresh
results. On any failure (no SDK, no credentials, network, empty response) it
falls back to the deterministic engine per step, so a replay never breaks. The
parity gate is unaffected either way, since narrative carries no `data-value`.

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
- `tests/test_bindings_reasoning.py` covers catalog loading, binding validation,
  and the deterministic reasoning aggregations.
- `tests/test_end_to_end.py` runs a scripted session in-process, saves, and
  regenerates as of a different date, asserting the structure holds while the
  data and the reasoning narrative change, and that Markdown comes from the same
  rendering spec.

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
   one headline occupancy number with a `data-value` attribute, and one
   `data-reasoning` placeholder. Request `formats: ["html", "md"]`.
7. Call `save_report_definition` with the transcript and the artifact. Confirm:
   status registered, parity passed, `scratch` absent from the definition, the
   stored SQL contains `__REPORT_DATE__` instead of literal dates, and the
   definition carries metric bindings and a reasoning step.
8. `python runner/regenerate.py --report-id <id> --as-of 2025-05-15` and open the
   output: same structure, different numbers, and a recomputed narrative. Both an
   `.html` and an `.md` file are written.
9. `pytest -q`, all green.

Or run the whole thing non-interactively: `python scripts/demo_session.py` then
`python runner/regenerate.py --report-id division_admissions_and_census --as-of 2025-05-15`.

## Project layout

```
data/seed.py           creates and populates poc.duckdb (data + knowledge graph)
server/main.py         FastMCP entry point
server/tools.py        the four MCP tools
server/intent_catalog.py   keyword intents for nl_query
server/call_log.py     tool-call log keyed by conversation_id
server/compiler.py     distillation: session record -> four-part definition
server/temporal.py     date literal detection and re-parameterization
server/knowledge_graph.py  metric/ValueSet catalog and binding validation
server/reasoning.py    ReasoningEngine protocol and deterministic engine
server/observability.py    local OTel-style spans to logs/spans.jsonl
server/parity.py       the parity gate
server/registry.py     definition registry (DuckDB tables)
server/db.py           shared DB path, connection, and constants
runner/regenerate.py   CLI replay runner
runner/render.py       HTML and Markdown rendering shared with the parity gate
templates/report_base.html.j2   base report template
tests/                 temporal, parity, bindings/reasoning, end-to-end
```

Owner identity is out of scope for this POC.
