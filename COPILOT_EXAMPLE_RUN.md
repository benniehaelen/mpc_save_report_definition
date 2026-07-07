# Copilot chat: a complete example run, word for word

This is a copy-paste script for doing a full re-generatable-report run with
**GitHub Copilot Chat in Agent mode**, using the `hin-poc` MCP tools.

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

`.github/copilot-instructions.md` is loaded automatically, so Copilot already
knows the tool workflow and the HTML artifact contract.

---

## Option A — one message (simplest)

Paste this single message into Copilot Chat (Agent mode) and approve each tool
call when prompted:

```text
Using the hin-poc MCP tools, build a complete re-generatable report and save it. Use one conversation_id for every call.

1. Run one throwaway query first named `scratch` (any count) and never reference it, to prove dead ends drop out.
2. Build "admissions by division" for the last 30 days ending at the anchor, executed as `admissions_by_division`.
3. Build "average census and occupancy rate by facility" for the same window, executed as `census_by_facility`.
4. Build a single overall occupancy rate for the same window, executed as `overall_occupancy`.

Assemble an HTML body fragment with:
- an <h1> title,
- the two tables using data-result="admissions_by_division" and data-result="census_by_facility",
- one headline: data-value="overall_occupancy.occupancy_rate",
- one empty reasoning paragraph: data-reasoning="occ_summary" data-over="census_by_facility.occupancy_rate" data-agg="max".

Build every cell and headline value directly from the exact numbers execute_sql returned. Then call save_report_definition with report_name "Division Admissions and Census", a short transcript, and final_artifact = {format:"html", title:"Division Admissions and Census", content:<the fragment>, formats:["html","md"]}.

Report back: status, parity.passed, report_id, and any warnings or unreplayable_sections.
```

---

## Option B — step by step (one message per turn)

Send these in order, waiting for Copilot to finish and approving tool calls each
time. This lets you watch each stage.

**Message 1 — start and explore:**

```text
Use the hin-poc tools with a single conversation_id for this whole session. Call nl_query for: "admissions by division, last 30 days". Show me the suggested_sql and the tables and columns it returned. Don't execute anything yet.
```

**Message 2 — validate and run the first query:**

```text
Refine that into SQL that returns division and admission count for the last 30 days ending at the anchor date. dry_run_sql it, fix any error, then execute_sql it as result_name "admissions_by_division". Show me the rows.
```

**Message 3 — second query:**

```text
Now build average midnight census and occupancy rate (census / bed_count) by facility for the same 30-day window. dry_run_sql then execute_sql it as "census_by_facility".
```

**Message 4 — headline query and a throwaway:**

```text
Execute one overall occupancy rate for the same window as "overall_occupancy". Also run one throwaway query as "scratch" that we will never reference, to show dead ends drop out of the definition.
```

**Message 5 — assemble the artifact:**

```text
Assemble an HTML body fragment: an <h1> title, the admissions_by_division and census_by_facility tables with matching data-result attributes, one headline element with data-value="overall_occupancy.occupancy_rate", and one empty paragraph with data-reasoning="occ_summary" data-over="census_by_facility.occupancy_rate" data-agg="max". Build all cell and headline text from the exact execute_sql values. Show me the fragment.
```

**Message 6 — save:**

```text
Call save_report_definition with report_name "Division Admissions and Census", a short transcript of this session, and final_artifact = {format:"html", title:"Division Admissions and Census", content:<the fragment above>, formats:["html","md"]}. Then report status, parity.passed, report_id, and any warnings or unreplayable_sections.
```

---

## After saving — replay it (VS Code terminal, not chat)

```
python runner/regenerate.py --list
python runner/regenerate.py --report-id <report_id from chat> --as-of 2025-05-15
```

Open `reports/<report_id>_v1_2025-05-15.html`: same structure, different numbers,
and a narrative sentence recomputed over the fresh data. An `.md` file is written
alongside it.

## What a successful run looks like

- `status: registered` and `parity.passed: true`.
- `scratch` does **not** appear in the saved definition.
- The stored SQL contains `__REPORT_DATE__` instead of literal dates.
- The definition carries metric bindings and one reasoning step.
- Replaying with a different `--as-of` produces new numbers and a new narrative
  while the tables and headline keep their structure.
