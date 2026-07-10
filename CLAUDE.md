# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local, cloud-free POC of **re-generatable reports**. A report is saved not as a
frozen document but as a *definition* (parameterized SQL + metric bindings +
reasoning steps + rendering spec) that a runner replays as of any date. An MCP
server (`hin-poc`) exposes four tools that a Copilot client drives to build and
save a report once; `runner/regenerate.py` replays it automatically. Everything
is offline: local DuckDB, no GCP, no network, no LLM in the core path.

## Commands

```bash
pip install -e .                  # core; add ".[dev]" for pytest+harlequin, ".[llm]" for anthropic
python data/seed.py               # (re)create data/poc.duckdb — idempotent, run before anything else
python server/main.py             # start the FastMCP stdio server (hin-poc)
python runner/regenerate.py --report-id <id> [--as-of 2025-06-15] [--formats html,md]
python runner/regenerate.py --list
python scripts/demo_session.py       # scripted v1 capture path (saves a report end-to-end)
python scripts/demo_market_story.py  # scripted v2 capture: charts, tabs, editorial blocks
pytest -q                         # full suite
pytest -q tests/test_parity.py::<name>   # single test
```

`pytest` auto-seeds a fresh DB once per session (`conftest.py`), so tests do not
require a manual `seed.py` run.

## Storage is split across two files, on purpose

- `data/poc.duckdb` — the analytic warehouse. **Only ever opened read-only**,
  which takes a *shared* lock, so the server, `regenerate.py`, `pytest` and
  `harlequin` can all attach at once. `server/db.get_connection()` defaults to
  `read_only=True`; keep it that way.
- `data/poc_meta.sqlite` — the two tables the server writes: `tool_call_log`
  (lineage) and `report_definitions` (the registry). SQLite in WAL mode allows
  one writer alongside many concurrent readers. Reached via
  `server/db.get_meta_connection()`, which is cached **per thread** because
  FastMCP dispatches tool calls on worker threads.

**Do not move a writable table back into the DuckDB file.** A read-write DuckDB
open takes an *exclusive* OS-level lock that refuses every other process,
read-only included — which is what used to force "stop the server before running
anything else," and what orphaned servers turned into apparently-hanging tools.

The one remaining exclusive lock is `data/seed.py`, which opens the warehouse
read-write to rebuild it. Nothing else may be attached while it runs; it also
resets the metadata store, so a reseed leaves no definitions pointing at rows
that no longer exist.

`server/db.execute_params` is a **DuckDB-only** workaround for a duckdb 1.5.4
prepared-statement deadlock under FastMCP's worker threads. SQLite has no such
bug, so the metadata store binds real `?` parameters. Do not route SQLite
queries through `execute_params`.

## Architecture

The system has two paths that meet at the `report_definitions` registry:

**Capture path** (interactive, via MCP tools in `server/tools.py`):
`nl_query` → `dry_run_sql` → `execute_sql` (logs lineage keyed by
`conversation_id`) → `save_report_definition`. The last tool runs
`compiler.distill` → `temporal.reparameterize` → `knowledge_graph.validate_bindings`
→ `parity.check`, and only calls `registry.register` if parity passes. It retries
distillation up to 3 times to reach parity before giving up with
`status: parity_failed`.

**Replay path** (automated, `runner/regenerate.py`): fetch definition → validate
bindings → bind `__REPORT_DATE__` to `--as-of` → execute named queries → run
reasoning steps over fresh results → render every format → write to `reports/`.

Key module map (`server/`): `tools.py` (the four tools + SELECT-only SQL guard),
`artifact.py` (v2 artifact parser — islands, value grammar, source-offset spans),
`compiler.py` (`Distiller` protocol — HTML lineage → four-part definition),
`linter.py` (rejects author-written templating, warns on frozen period labels),
`temporal.py` (rewrites date literals to `__REPORT_DATE__` offsets, quarter
boundaries to `DATE_TRUNC` expressions), `parity.py` (the gate — compares
extracted *data values*, not markup), `knowledge_graph.py` (metrics/ValueSets
catalog + binding validation), `reasoning.py` (`ReasoningEngine` protocol),
`registry.py` (definition storage in the SQLite metadata store), `db.py` (both DB
paths, cached connections, `ANCHOR_DATE`), `observability.py` (OTel-style spans to
`logs/spans.jsonl`). `runner/render.py` holds the HTML/Markdown renderers shared
by the parity gate and the runner, plus the Jinja filters/globals (`pick`,
`sign_class`, `editorial_banner`) the v2 templates call.

### Design principles to preserve

- **The core path stays offline and LLM-free.** No GCP, no network, no LLM calls.
- Both the compiler (`Distiller`) and reasoning engine (`ReasoningEngine`) are
  deterministic implementations behind protocols. **Prefer swapping an
  implementation behind the protocol over editing call sites.** LLM-backed
  reasoning (`AnthropicReasoningEngine`) is opt-in via `POC_REASONING=anthropic`
  and falls back to the deterministic engine per step on any failure.
- Synthetic data is seeded with a fixed random seed and anchored at **2025-06-30**
  (`db.ANCHOR_DATE`, kept in sync with `data/seed.py`) so parity results are
  stable. Both must change together.
- Optional env config loads from a gitignored `.env` via `server/env.py`; real
  environment variables take precedence.

## The HTML artifact contract

When building a report artifact to pass to `save_report_definition`, the compiler
recovers lineage from `data-result` / `data-value` / `data-reasoning` attributes
on a body-fragment of HTML. This contract is documented in full in
**`.github/copilot-instructions.md`** and `README.md` ("how the client must build
the HTML artifact") — read it before authoring or debugging an artifact. Parity
compares extracted numbers, so cell/headline text must be built verbatim from
what `execute_sql` returned. Convenience builders: `runner/render.py`
(`build_table_html`, `build_value_span`).

There are **two contracts, and they never mix**. `artifact.is_v2()` sniffs the
markers and `distill`/`parity.check` dispatch on it. This is load-bearing, not
stylistic: the v1 `data-value` regex parses `race[last].gap | thousands` as
result `race[last]`, field `gap | thousands`, so a v2 artifact reaching the legacy
path is corrupted silently rather than rejected loudly. The v2 contract adds JSON
data islands, a selector/filter value grammar, declarative charts and bound
tables, goal-directed reasoning, verbatim editorial blocks with staleness
watches, and a tabbed layout — see the README section "The v2 artifact contract".

Two v2 rules worth internalising:

- **Computation stays in SQL.** `templates/runtime/charts_v1.js` draws and
  formats; it never derives. Gaps, shares, deltas, display strings, and sort order
  are columns and `ORDER BY` clauses. Give every value-ordered `ORDER BY` a
  tiebreaker — ties come back in an arbitrary order and parity compares islands
  positionally.
- **The v2 compiler splices the original source at each node's offsets** rather
  than re-serializing a BeautifulSoup tree, so untouched markup stays
  byte-identical and editorial `html_sha256` values stay stable.