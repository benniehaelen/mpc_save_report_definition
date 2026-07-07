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
python scripts/demo_session.py    # scripted non-interactive capture path (saves a report end-to-end)
pytest -q                         # full suite
pytest -q tests/test_parity.py::<name>   # single test
```

`pytest` auto-seeds a fresh DB once per session (`conftest.py`), so tests do not
require a manual `seed.py` run.

## Critical gotcha: the DuckDB lock

The server holds an **exclusive read-write lock** on `data/poc.duckdb`. DuckDB
will not let a second process open that file while the lock is held — not even
read-only. **Stop the MCP server before running `regenerate.py`, `seed.py`, the
tests, or `harlequin`.** The runner detects this and prints a "database is
locked" message instead of a raw traceback.

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
`compiler.py` (`Distiller` protocol — HTML lineage → four-part definition),
`temporal.py` (rewrites date literals to `__REPORT_DATE__` offsets), `parity.py`
(the gate — compares extracted *data values*, not markup), `knowledge_graph.py`
(metrics/ValueSets catalog + binding validation), `reasoning.py` (`ReasoningEngine`
protocol), `registry.py` (definition storage in DuckDB), `db.py` (shared DB path,
cached connection, `ANCHOR_DATE`), `observability.py` (OTel-style spans to
`logs/spans.jsonl`). `runner/render.py` holds the HTML/Markdown renderers shared
by the parity gate and the runner.

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