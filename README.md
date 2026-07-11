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

Each tool accepts an optional `conversation_id` session key (see "Session
correlation" below); the arguments that matter are listed first.

- **nl_query(question)** maps a natural language question to a matched intent, the
  relevant tables with their columns, and a suggested SQL template for the last 30
  days ending at the anchor. Executes nothing.
- **dry_run_sql(sql)** validates a single SELECT and returns its result columns.
  Not recorded as lineage.
- **execute_sql(sql, result_name)** runs a validated SELECT (capped at 500 rows),
  assigns it `result_name`, logs the call, and returns the rows. The `result_name`
  is what makes named results work later.
- **save_report_definition(report_name, transcript, final_artifact,
  temporal_confirmations=None)** distills a named query set from the logged calls,
  runs the parity gate against the final artifact, and registers the definition on
  pass. Its response carries a `session` block naming the correlation key used.

### Session correlation

Every logged call is keyed so `save_report_definition` can gather the queries a
report was built from. The key is resolved by a fallback chain, so a client no
longer has to invent and repeat one consistently:

1. **Explicit** — a non-empty `conversation_id` argument wins, always. This stays
   the contract for the demo scripts, the tests, and any non-Copilot client.
2. **`_meta`** — otherwise the key is taken from the client's MCP `_meta` trace id.
   VS Code Copilot sends `vscode.conversationId`, stable for the whole chat, so all
   its calls correlate automatically. The field list is `POC_CORRELATION_META_KEYS`
   (comma-separated, default `vscode.conversationId`); a meta-derived key is
   prefixed `meta-`.
3. **Generated** — otherwise a `gen-<uuid>` key is minted and every response warns
   that correlation was not established.

A save whose key has no logged calls (the symptom of a chat reload that rotated
the id) returns `status: no_logged_calls` naming the key and the remedy, rather
than a mystifying empty definition. Run the server with `POC_LOG_META=1` and
`python scripts/analyze_meta_probe.py` to inspect what `_meta` a client actually
sends.

> Production note: on a shared, multi-tenant server the key must be namespaced by
> the authenticated principal (`key = f"{principal}:{value}"`) — a client-supplied
> id is a claim, not an identity. The id correlates; authentication authorizes.
> `server/correlation.py` documents this but does not implement it (single-user POC).

## Copilot session (capture path), step by step

The capture path is interactive: a Copilot client (Claude Code, GitHub Copilot in
VS Code, or any MCP-capable assistant) drives the four tools to build a report,
then saves it once. The replay path is then fully automated.

### 1. Connect the MCP server to your Copilot client

Seed the database first (`python data/seed.py`). The server speaks MCP over
stdio. For GitHub Copilot in VS Code, add `.vscode/mcp.json`:

```json
{
  "servers": {
    "hin-poc": {
      "type": "stdio",
      "command": "C:\\src\\mpc_save_report_definition\\.venv\\Scripts\\python.exe",
      "args": ["C:\\src\\mpc_save_report_definition\\server\\main.py"]
    }
  }
}
```

For Claude Code, use the `mcpServers` form shown under "Running the MCP server"
above. Start a fresh chat and confirm the four `hin-poc` tools are listed.

### 2. Give the Copilot these instructions

Paste this into the Copilot so it produces a parity-safe artifact (verbatim):

> You have the `hin-poc` MCP tools. To build a re-generatable report:
> 1. Reuse one `conversation_id` string for every tool call in this session.
> 2. Call `nl_query` with the user's question. Read `suggested_sql` and the
>    returned table and column lists.
> 3. Refine the SQL, then `dry_run_sql` it. Fix any error and dry-run again.
> 4. `execute_sql` with a clear snake_case `result_name` (for example
>    `admissions_by_division`). Repeat for each result the report needs. Do not
>    reference throwaway queries in the report; they drop out automatically.
> 5. Build the report as an HTML body fragment where: every data table is
>    `<table data-result="<result_name>">` with a `<thead>` of column names and a
>    `<tbody>` whose cells are the exact values `execute_sql` returned; every
>    headline number is `<span data-value="<result_name>.<field>">value</span>`;
>    every recomputed sentence is an empty `<p data-reasoning="<id>"
>    data-over="<result_name>.<field>" data-agg="max|min|avg|total"></p>`.
> 6. Call `save_report_definition` with `report_name`, the `transcript`
>    (`[{role, content}]`), and `final_artifact = {format: "html", title,
>    content, formats: ["html", "md"]}`.
> 7. Report back `status`, `parity.passed`, `report_id`, and any `warnings` or
>    `unreplayable_sections`.

### 3. The interactive loop, in short

1. Ask a natural-language question ("admissions by division, last 30 days").
2. `nl_query` -> refine SQL -> `dry_run_sql` -> `execute_sql` as a named result.
3. Repeat for each result the report needs.
4. Assemble the artifact per the contract in the next section.
5. `save_report_definition`. On `status: registered` with `parity.passed: true`,
   the report is replayable with `runner/regenerate.py --report-id <id>`.

If parity fails, the response names the first differing table or value; the usual
cause is cell text that does not match the query output (see the contract below).

## Three artifact modes

`artifact.detect_mode(html)` sniffs which contract a submitted artifact follows,
and `save_report_definition` routes on the answer. The modes never mix.

| Mode | What it looks like | What the server does |
|---|---|---|
| `legacy` | populated `<table data-result>` bodies, `data-value="result.field"` | v1 compiler |
| `v2` | JSON islands, selector/filter values, `data-chart`, editorial blocks | v2 compiler |
| `free_form` | no `data-*` at all: JS constants, hand-rolled SVG, plain prose | **extract structure, then v2 compiler** |

**Free-form is the mode you get for free.** Build the page however you like. At
save time the server fingerprints every number on it against what `execute_sql`
returned, rewrites the page into a v2 artifact, and hands it to the same compiler
and parity gate everything else goes through. The one discipline that remains:

> **Every displayed number must be a value `execute_sql` returned.**

That is what keeps extraction deterministic. A number the page computed in
JavaScript has no lineage, so it cannot be replayed — the server will say so
rather than freeze it silently.

Emitting the v2 contract yourself is still supported and still **preferred** when
it is cheap to do: a contract artifact skips the confirmation round-trip below.

### What extraction can prove, and what it must ask about

A **fingerprint match** is fact. A JS constant whose rows equal a logged result's
rows *is* that result; no one needs to confirm it. Such a page registers in one
call, offline, with no LLM:

```bash
python scripts/demo_free_form.py     # POC_DISTILLER unset: one call, no network
```

Anything **inferred** — a derived query, a prose block reclassified as
regenerable, an island the model placed by guesswork — is never registered
silently. The first call returns the proposal instead:

```jsonc
{
  "status": "needs_structure_confirmation",
  "extraction": {
    "matched_islands":  [{"result_name": "race_quarters", "source": "const RACE"}],
    "derived_queries":  [{"result_name": "race_gap", "sql": "SELECT ...", "covers": ["const:RACE_GAP"]}],
    "narrative":        [{"block_id": "b2", "tier": "editorial", "excerpt": "Thesis: HCA can ..."}],
    "unmatched":        ["const MYSTERY (14 rows x 2 columns)"]
  },
  "confirmation_token": "<sha256 of the plan>"
}
```

Call again with the token to apply it:

```python
save_report_definition(..., structure_confirmations=[{"token": "...", "accept_all": True}])
```

Or override individual items — flip a block back to frozen prose, attach a
staleness watch, reject a derived query:

```python
structure_confirmations=[
    {"token": "..."},
    {"derived": "race_gap", "accept": False},
    {
        "block_id": "b1",
        "tier": "editorial",                       # computed | analytical | editorial
        "watch": "kpi_summary[0].gap_now < 800",   # optional, editorial blocks only
        "authored_as_of": "2025-06-30",            # optional, ISO YYYY-MM-DD
    },
]
```

A **watch** is what makes frozen prose honest: the block replays verbatim, and
when its condition fires at a later replay the runner prepends an amber staleness
banner. It belongs only on an editorial block — an analytical block is regenerated
from fresh data anyway, so there is no frozen judgment left to go stale.

A **fingerprint-clean page needs no token to carry a watch.** There is nothing to
confirm, and a watch is a directive rather than an inference, so send the override
on the first call and it registers in one:

```python
save_report_definition(..., structure_confirmations=[
    {"block_id": "b2", "tier": "editorial", "watch": "kpi_summary[0].gap_now < 800"},
])
```

If the plan turns out to hold an inference after all, that call is refused and you
are told to fetch a token first — inference is exactly what a token exists to put
in front of a human.

Unlike a model's proposal, an override is an explicit human decision, so a bad one
**fails the call** rather than being quietly dropped. Any of these returns
`invalid_structure_confirmation` and registers nothing: an unparseable watch, a
watch on a non-editorial block, an unknown `block_id`, or a non-ISO
`authored_as_of`. **The token is not consumed**, so fix the entry and retry with
the same token.

The watch's *reference* is checked by the parity gate, not by the override path: a
watch naming a result the report does not carry comes back as `parity_failed` with
the declaration named.

The plan is cached under its token, so the second call applies exactly what you
were shown; the model is not asked again.

A token names one **proposal**, and it is **single-use**:

| Situation | Status returned |
|---|---|
| unknown token, or a token from another conversation | `structure_confirmation_expired` |
| the artifact changed since the proposal was made | `structure_confirmation_stale` |
| the token already registered a report | `structure_confirmation_used` (names that report) |
| no token, or a bad narrative override | `invalid_structure_confirmation` |

None of these register anything. A plan is a set of source offsets into the page
it was proposed against, so applying it to an edited page would splice at the
wrong bytes — hence the staleness check. A parity failure leaves the token
*unspent*, so you can fix the artifact and retry the same confirmed plan.

Two guarantees hold whichever engine proposed the plan. The server **executes**
every proposed derived query and checks it really does reproduce the numbers it
claims to cover, dropping it with a warning if not. And a proposal may never
contradict a fingerprint match — it can only fill in what fingerprinting could not
reach. Whatever is still unmatched at the end surfaces in `warnings` and
`unreplayable_sections`, never in silence.

Set `POC_DISTILLER=anthropic` to let a model propose derived SQL, charts, and
narrative tiers. It is opt-in, falls back to the deterministic extractor on any
failure, and changes nothing about what the server will accept.

## IMPORTANT: how the client must build the HTML artifact

This section describes the **v1 (legacy)** contract. The compiler recovers a
report's lineage from the HTML you submit. For a section to be replayable, the
artifact must carry data attributes that tie rendered content back to named query
results:

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
`build_value_span`, `build_reasoning_para`) if you want to construct a
parity-safe artifact directly.

To see the report *before* you save it, write the assembled body fragment to a
file and run `python scripts/preview_artifact.py <fragment.html>`. It wraps the
fragment in the same base template the runner uses and opens the page in your
browser, so you can inspect the original artifact — the tables and headline
numbers the parity gate will lock — ahead of `save_report_definition`. It only
reads the template, so it is safe to run while the server is up.

## The v2 artifact contract: charts, tabs, and editorial

The contract above is enough for a table-and-headline report. A chart-heavy,
tabbed, narrative-rich dashboard needs more, so the compiler accepts a second set
of markers. **The two contracts never mix**: use any v2 marker and the whole
artifact takes the v2 path. Existing v1 artifacts are unaffected.

Build v2 artifacts with the builders in `runner/render.py`: `build_island`,
`build_value_span_v2`, `build_chart_div`, `build_bound_table`,
`build_reasoning_block`, `build_editorial_block`, `build_tabs`.

### Data islands

Each query result is embedded once, as JSON, and everything else refers to it:

```html
<script type="application/json" data-result="race_quarters">[{"qtr":"Q1'25","gap":662}]</script>
```

The compiler replaces the body with `{{ race_quarters.rows | tojson }}`. The
parity gate compares islands row by row and field by field, so an island is the
unit of truth for a v2 report. Select only JSON-serializable columns — a raw
`DATE` becomes an ISO string, and anything more exotic fails loudly at render.

### The value grammar

`data-value` takes a selector and an optional filter chain:

| Reference | Row it picks |
|---|---|
| `result.field` | first row (as in v1) |
| `result[3].field` | row 3 |
| `result[first].field` / `result[last].field` | first / last row |
| `result[col='val'].field` | the row where `col` equals `val` (`''` escapes a quote) |

Filters, and only these: `thousands`, `signed`, `pct(n)`, `pp`, `round(n)`. They
format; they never derive. Add `data-style="sign"` to colour the element by the
value's sign (`growth` / `decline` / `flat`).

```html
<span data-value="race_quarters[last].gap_trend | signed | thousands" data-style="sign">-203</span>
```

### Charts and bound tables

Both are declarations. The markup ships empty and `templates/runtime/charts_v1.js`
fills it in the browser.

```html
<div id="raceChart" data-chart='{"type":"line","result":"race_quarters","x":"qtr",
     "series":[{"field":"gap","label":"Gap","color":"#E75925"}]}'></div>

<table data-result="race_quarters"
       data-columns="qtr:Quarter, gap:Gap|thousands, gap_trend:Trend|signed|thousands|style:sign">
  <thead><tr><th>Quarter</th><th>Gap</th><th>Trend</th></tr></thead>
  <tbody></tbody>
</table>
```

Chart types are `line`, `bar`, and `diverging_bar`; each accepts an optional
`filter: {col: val}`. The `<tbody>` must be empty — the parity gate never runs
JavaScript, so an authored row would be compared against nothing.

**Computation stays in SQL.** The runtime draws and formats. If a chart needs a
gap, a share percentage, a delta, a display string, or a particular row order,
add a column or an `ORDER BY` to the query. Give every value-ordered `ORDER BY` a
tiebreaker: ties come back in an arbitrary order, and the parity gate compares
islands positionally.

### Reasoning v2

A step states a goal over named inputs instead of naming one aggregate:

```html
<p data-reasoning="race_story" data-goal="Explain how the gap moved."
   data-inputs="race_quarters, esl_quarters[esl='ORTHOPEDICS']" data-max-sentences="3"></p>
```

### Editorial blocks

Author-written prose that replays **verbatim** and is hashed, not recomputed. An
optional `data-watch="valueref OP number"` is re-evaluated at every replay; when
it fires, the runner prepends an amber staleness banner. An unresolvable watch
banners the block rather than crashing the replay.

```html
<div data-editorial="thesis" data-authored-as-of="2025-06-30"
     data-watch="kpi_summary[0].gap_now < 800"><strong>Thesis: …</strong></div>
```

### Theme and layout

Set `final_artifact["layout"] = "tabbed-dashboard"` and
`final_artifact["theme"] = "market-story-v1"`. Declare the tabs with
`<nav data-tabs='[{"id":"race","label":"The Race"}]'></nav>` and mark each content
element with `data-section="race"`; anything without a `data-section` (the KPI
strip, the islands) renders above the tab bar. The theme CSS and the chart
runtime are inlined, so a rendered report is one self-contained file.

**A tabbed layout renders HTML only.** Markdown has no tabs, no SVG, and no
runtime to fill the tables, so `formats` is forced to `["html"]` and a warning is
recorded. `--formats md` on such a report prints that warning and skips.

### The linter

The compiler rejects `{{ }}` / `{% %}` in a submitted artifact (it writes the
template; you supply the data) and non-whitelisted filters. It *warns* about a
literal period label such as `Q1'25` in a heading or `.kpi-label`: that text is
frozen the day it is written and lies at every later replay. Bind it to a result
with `data-value` instead. A quarter inside a `data-value` span or an editorial
block is fine.

### Preview

```bash
python scripts/preview_artifact.py fragment.html --layout tabbed-dashboard
```

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

The runner can be run while the MCP server is up. `data/poc.duckdb` is only ever
opened read-only, and a read-only DuckDB connection takes a shared lock that any
number of processes may hold at once. The two tables the server writes -- the
lineage log and the definition registry -- live in `data/poc_meta.sqlite`, which
uses SQLite's WAL mode to admit one writer alongside concurrent readers.

The one exception is `python data/seed.py`, which opens the warehouse read-write
to rebuild it and therefore takes an exclusive lock. Nothing else may be attached
while it runs.

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
copy .env.example .env            # then edit .env and paste your key
python runner/regenerate.py --report-id <id> --as-of 2025-05-15
```

The `.env` file (gitignored) is loaded automatically at startup; set
`POC_REASONING=anthropic` and `ANTHROPIC_API_KEY` there. Alternatively export
those as environment variables. Real environment variables take precedence over
`.env`.

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

**Quarter boundaries get their own grain.** A quarter is not a fixed number of
days, so rewriting `DATE '2021-04-01'` as a day offset drifts off the boundary as
the report date moves. Confirm such a literal with
`{"literal": "2021-04-01", "treatment": "relative_quarter"}` and it becomes
`DATE_TRUNC('quarter', __REPORT_DATE__) - INTERVAL 48 MONTH`, which lands on a
quarter start for every report date. Past quarter starts within a year are
rewritten this way even unconfirmed; the current and future ones are not, since
an exclusive upper bound is usually written as the day after the period ends.

## Tests

```
pytest -q
```

The suite seeds a fresh database once and pins `POC_REASONING=heuristic`, so it
never needs the network even if your `.env` opts into the LLM engine.

- `tests/test_temporal.py` covers literal detection, relative rewriting, fixed
  passthrough, the conservative fallback, and the quarter-grain rewrite (including
  a bind-and-execute round trip through DuckDB).
- `tests/test_parity.py` / `tests/test_parity_v2.py` cover a matching artifact
  passing, an edited cell (or island field) failing and being named, markup-only
  differences passing, filter-aware value comparison, and the reference-completeness
  checks that catch a chart pointing at a column that no longer exists.
- `tests/test_artifact.py` covers the v2 parser: every selector and filter, the
  source-offset spans, and the marker that separates v1 from v2.
- `tests/test_compiler_v2.py` covers island/value/reasoning/editorial rewriting,
  the linter, and a guard that a legacy artifact still distills exactly as before.
- `tests/test_render_v2.py` covers the filters, `pick`, `tojson` over DuckDB
  scalars, the tabbed layout, and a golden check on the legacy render.
- `tests/test_bindings_reasoning.py` covers catalog loading, binding validation,
  and both the v1 and v2 deterministic reasoning engines.
- `tests/test_editorial.py` covers verbatim replay, the watch condition firing at
  one replay date and not another, and unresolvable watches degrading to a banner.
- `tests/test_marketshare_seed.py` asserts the seeded story: the gap narrows, HCA
  gains share in 12 of 17 service lines, orthopedics loses share in a growing
  market, and the new entrant arrives in the final three quarters.
- `tests/test_end_to_end.py` and `tests/test_end_to_end_market.py` run scripted
  sessions in-process, save, and regenerate as of a different date, asserting the
  structure holds while the data, the KPIs, and the narrative change.

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

### The complex-report walkthrough

`scripts/demo_market_story.py` captures the same way, but exercises the v2
contract end to end: nine quarter-windowed queries, nine data islands, twelve
charts, four runtime-bound tables, three goal-directed reasoning steps, two
editorial blocks, and a six-tab layout.

```bash
python scripts/demo_market_story.py                                            # -> status: registered
python runner/regenerate.py --report-id las_vegas_race_to_1 --as-of 2025-06-30  # banner present
python runner/regenerate.py --report-id las_vegas_race_to_1 --as-of 2025-03-31  # banner absent
python runner/regenerate.py --report-id las_vegas_race_to_1 --formats md        # warns, skips md
```

Open the two HTML files side by side. The tabs switch, the charts draw, and the
window slides back one quarter: the race chart starts at `Q1'21` instead of
`Q2'21`, the gap KPI reads 865 instead of 662, and the narrative is rewritten
from the fresh numbers. The editorial prose is byte-identical in both — but only
the 2025-06-30 render carries the amber staleness banner, because its watch
condition (`gap_now < 800`) is true at 662 and false at 865.

## Project layout

```
data/seed.py           creates and populates poc.duckdb (data + knowledge graph)
server/main.py         FastMCP entry point
server/tools.py        the four MCP tools
server/intent_catalog.py   keyword intents for nl_query
server/call_log.py     tool-call log keyed by conversation_id
server/artifact.py     v2 artifact parser: islands, value grammar, source offsets
server/compiler.py     distillation: session record -> four-part definition
server/linter.py       rejects author-written templating; warns on frozen labels
server/temporal.py     date literal detection and re-parameterization
server/knowledge_graph.py  metric/ValueSet catalog and binding validation
server/reasoning.py    ReasoningEngine protocol and deterministic engine
server/observability.py    local OTel-style spans to logs/spans.jsonl
server/parity.py       the parity gate
server/registry.py     definition registry (DuckDB tables)
server/db.py           shared DB path, connection, and constants
runner/regenerate.py   CLI replay runner
runner/render.py       HTML and Markdown rendering shared with the parity gate
scripts/demo_session.py        scripted v1 capture
scripts/demo_market_story.py   scripted v2 capture (the Las Vegas report)
scripts/market_queries.py      the nine market-share queries; all derivation in SQL
templates/report_base.html.j2       base report template
templates/layouts/tabbed_dashboard.html.j2   the tabbed layout
templates/themes/market_story_v1.css         theme inlined into the report
templates/runtime/charts_v1.js               draws charts, fills tables, no derivation
tests/                 temporal, parity, artifact, compiler, render, editorial,
                       seed story, and both end-to-end walkthroughs
```

Owner identity is out of scope for this POC.
