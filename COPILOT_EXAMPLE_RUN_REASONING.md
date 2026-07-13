# Copilot chat: an example run focused on reasoning steps, word for word

A copy-paste script for building a re-generatable report whose highlight is its
**reasoning steps** — the sentences the runner recomputes over fresh results at
replay, so the narrative changes with the report date. Uses **GitHub Copilot Chat
in Agent mode** and the `hin-poc` MCP tools.

`.github/copilot-instructions.md` loads automatically, so Copilot already knows
the tool workflow and the HTML artifact contract (including the reasoning markup
and the `final_artifact` shape). The prompts below name what each reasoning step
should say and leave the attributes to Copilot's instructions.

Your instructions already define reasoning steps and the four aggregations
(`max`/`min`/`avg`/`total`) and note that reasoning prose is not parity-checked,
so the prompts below just name each step's result, field, and aggregation. This
example wires up **one of each aggregation** so you can watch all four sentences
recompute across replay dates.

## Before you open chat (one time)

1. Seed the database (already done if `data/poc.duckdb` exists):

   ```
   python data/seed.py
   ```

2. Make sure `.vscode/mcp.json` registers the server, then start it: open
   `.vscode/mcp.json` and click **Start** on the `hin-poc` entry (or run
   **MCP: List Servers** from the Command Palette and start it).

3. In Copilot Chat, switch the mode dropdown to **Agent**, open the **🛠 tools**
   picker, and confirm these four tools are enabled:
   `nl_query`, `dry_run_sql`, `execute_sql`, `save_report_definition`.

---

## Option A — one message (simplest)

Paste this single message into Copilot Chat (Agent mode) and approve each tool
call when prompted:

```text
Using the hin-poc MCP tools, build a re-generatable report focused on narrative, and save it. Do not pass a conversation_id; the session is correlated automatically.

1. Build "admissions by division" for the last 30 days ending at the anchor, executed as `admissions_by_division` (columns: division, admissions).
2. Build "average census and occupancy rate by facility" for the same window, executed as `census_by_facility` (columns: facility_name, avg_census, occupancy_rate).

Assemble the report as an HTML body fragment following the artifact contract in your instructions:
- an <h1> title,
- a table for admissions_by_division and a table for census_by_facility,
- then FOUR recomputed reasoning sentences, one empty element each:
  - "peak_occupancy": over census_by_facility.occupancy_rate, aggregated as max,
  - "low_occupancy": over census_by_facility.occupancy_rate, aggregated as min,
  - "mean_occupancy": over census_by_facility.occupancy_rate, aggregated as avg,
  - "total_admissions": over admissions_by_division.admissions, aggregated as total.

Then save it with save_report_definition: report_name "Facility Occupancy Narrative", a short transcript, and output formats html and md.

Report back: status, parity.passed, report_id, and the four reasoning sentences the save produced.
```

To see the report *before* it is saved, add a line asking Copilot to "write the
assembled fragment to `reports/_artifact.html` and stop before saving", then
preview it with `python scripts/preview_artifact.py reports/_artifact.html`
(see Message 4 under Option B).

---

## Option B — step by step (one message per turn)

Send these in order, waiting for Copilot to finish and approving tool calls each
time.

**Message 1 — first query:**

```text
Use the hin-poc tools (no conversation_id needed; the session correlates automatically). Build admissions by division for the last 30 days ending at the anchor date (columns division, admissions). dry_run_sql it, then execute_sql it as "admissions_by_division". Show me the rows.
```

**Message 2 — second query:**

```text
Now build average midnight census and occupancy rate (census / bed_count) by facility for the same 30-day window (columns facility_name, avg_census, occupancy_rate). dry_run_sql then execute_sql it as "census_by_facility".
```

**Message 3 — assemble with four reasoning steps:**

```text
Assemble the report as an HTML body fragment per the artifact contract in your instructions: an <h1> title, tables for admissions_by_division and census_by_facility, then four empty reasoning paragraphs:
- "peak_occupancy" over census_by_facility.occupancy_rate, agg max,
- "low_occupancy" over census_by_facility.occupancy_rate, agg min,
- "mean_occupancy" over census_by_facility.occupancy_rate, agg avg,
- "total_admissions" over admissions_by_division.admissions, agg total.
Show me the fragment.
```

**Message 4 — preview the report before saving:**

You have the fragment, but not a rendered page yet — the base page shell (the
styling) is only added at render time. Preview it so you can see the *original
artifact* before it is saved and parity-checked. Ask Copilot:

```text
Write the exact HTML fragment above to reports/_artifact.html.
```

Then in the VS Code terminal — the server can stay running, since this only
reads the base template and never touches the database:

```
python scripts/preview_artifact.py reports/_artifact.html
```

It wraps the fragment in the report's base template, writes
`reports/_preview.html`, and opens it in your browser. The two data tables are
what the parity gate will lock; all four reasoning paragraphs are blank here
(shown as "↻ recomputed at replay time") because their narrative is generated at
replay, not at save.

**Message 5 — save:**

```text
Call save_report_definition with report_name "Facility Occupancy Narrative", a short transcript of this session, the fragment above as the artifact content, and output formats html and md. Then report status, parity.passed, report_id, and the four reasoning sentences the save produced.
```

---

## After saving — replay it (VS Code terminal, not chat)

You can leave the MCP server running: the runner opens the warehouse read-only,
which takes a shared lock, and reads the definition from the SQLite metadata
store.

```
python runner/regenerate.py --list
python runner/regenerate.py --report-id facility_occupancy_narrative --as-of 2025-05-15
python runner/regenerate.py --report-id facility_occupancy_narrative --as-of 2025-06-30
```

Open the two HTML files and compare: the tables and all **four narrative
sentences** differ between the dates, because each step is recomputed over that
date's freshly executed results.

## What a successful run looks like

- `status: registered` and `parity.passed: true`.
- The stored SQL contains `__REPORT_DATE__` instead of literal dates.
- The definition carries **four `reasoning_steps`** (`peak_occupancy`,
  `low_occupancy`, `mean_occupancy`, `total_admissions`), each with its
  `result_name`, `field`, and `agg`.
- Each replay date yields a different set of four sentences — the highest/lowest
  facility, the average occupancy, and the total admissions all move with the
  data.
