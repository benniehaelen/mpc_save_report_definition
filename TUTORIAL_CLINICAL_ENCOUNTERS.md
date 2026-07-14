# HIN 2.0 re-generatable reports
## Runbook: eight lessons, from one clinical table to an encounters dashboard

**Audience:** anyone who has done the *One table to market story* tutorial (or Part 1 of the
main runbook) and wants to build re-generatable reports over the **clinical** tables — the
`encounter` fact table and the facility master.
**Companion to:** *HIN 2.0 Tutorial — One table to market story* (this runbook is its parallel
for the clinical schemas).

This runbook teaches the same system as the market-story tutorial, over a different, larger
dataset: patient **encounters** for Tennessee hospitals. Each lesson adds one idea to the
previous report, then saves and replays it and watches what moves. Every prompt is
paste-verbatim into Copilot Chat (Agent mode); lessons 1–7 share one chat, lesson 8 starts a
fresh one — the prompts say which.

### Before you start

Do Part 1 of the main runbook once (venv, `pip install -e .`, `python data/seed.py`, register
and start the `hin-poc` server, confirm the four tools in Copilot's picker). The seed builds the
clinical data and, on top of it, one curated view made for exactly this kind of demo:

**`encounters`** — one clean row per current patient encounter, with the columns you would
expect: `encounter_date`, `facility`, `state`, `facility_type`, `time_zone`, plus `coid` and
`encounter_id`. You query it in plain English, and `nl_query` lists it like any other table.
Under the hood it already did the hard parts — joined the raw encounter records to the facility
master, turned the text admission timestamp into a real date, kept only the current version of
each record, and dropped incomplete rows — so you never have to think about any of that.

> *For the curious (and skippable): the raw tables are still there —
> `clinical_core_silver.encounter`, `pub_facility_master_silver.facility_master_sites_silver`, and
> the `enterprise_ontology_gold.facility_master_site` view — and `encounters` is built from them.
> You can query them by full name, but nothing in this runbook needs you to.*

One promise, same as the other tutorial: **you never write HTML.** You describe what the report
should show; the agent writes the markup from `.github/copilot-instructions.md`. And **a session
is a chat** — the server correlates tool calls through your client's trace id automatically, so
you never manage a conversation id; just keep one report's work in one chat. When a lesson says
"same chat," stay; when it says "new chat," start one.

The ladder:

| Lesson | You build | The one new idea |
|---|---|---|
| 1 | Monthly encounters, one table | named results, the parity gate; the window slides on replay |
| 2 | + a headline total | bound numbers from a single-row result |
| 3 | + a summary written by reasoning | narrative that regenerates at every replay |
| 4 | + a trend chart | charts as declarations, not code |
| 5 | Encounters by facility | a second result and table — the building block of a dashboard |
| 6 | + a thesis with a watch | editorial judgment that flags its own staleness |
| 7 | A two-tab clinical dashboard | themes, layouts, sections — composition |
| 8 | The same, built free-form | save-time extraction: fingerprints, proposals, confirmation |

---

## Lesson 1. One table: monthly encounters

**What you learn:** a report is a definition. You will run one query, wrap its result in the
smallest possible report, and replay it at an earlier date — the 24-month window slides on its
own.

**Paste into Copilot Chat**

> Use the hin-poc tools. From the `encounters` view, build a monthly count of encounters for
> **Tennessee hospitals** (state TN, facility type starting "Hospital") over the **last 24 months
> ending at the anchor date, 2025-06-30** — one row per calendar month labelled like `2025-06`,
> ordered by month. `dry_run_sql` it, then `execute_sql` it as `monthly_encounters`. Show me the
> rows.

Twenty-five monthly rows come back (2023-06 through 2025-06; the window's lower edge lands before
the data starts, so the first month with encounters is 2023-06). Each month is a few hundred
encounters. Notice you described this in one sentence — the view already handled the join, the
date conversion, and the "count each encounter once" rule. Now the smallest possible report:

**Paste into Copilot Chat**

> Assemble a minimal report per the contract in your instructions: a heading "Monthly TN hospital
> encounters" and one table of `monthly_encounters`, columns Month and Encounters, the encounters
> formatted with thousands separators. Don't type the numbers — let the report format them from
> the result. Save with `save_report_definition` as report_name "Clinical 1 Monthly Encounters",
> formats html. Report status, parity, and the stored SQL.

**What you should see**

```
status: registered   report_id: clinical_1_monthly_encounters   definition_version: 1
parity: passed on attempt 1 — "all islands and values match"
session: {"correlation_key": "meta-…", "source": "meta", "logged_calls": 1}
stored SQL: … BETWEEN date_trunc('month', __REPORT_DATE__ - INTERVAL 24 MONTH)
                  AND __REPORT_DATE__ …
```

Read the stored SQL twice: **both** `DATE '2025-06-30'` literals became `__REPORT_DATE__`. The
window has two edges and both are now relative to the report date. Replay six months earlier
(the runner opens the warehouse read-only, so you can leave the server running):

**Terminal**
```
python runner/regenerate.py --report-id clinical_1_monthly_encounters --as-of 2024-12-31
```

Open the output: the same table, but now it ends at **2024-12** — nineteen monthly rows, because
the 24-month window slid back with the report date. Nobody re-ran anything; the definition did.

*(A browser fills this table: it is a bound table, so the numbers appear when you open the HTML in
a browser, not in the raw source.)*

---

## Lesson 2. A headline total

**What you learn:** a standalone number is bound to its source, never typed.

**Paste into Copilot Chat**

> Same chat. Execute one more query as `encounter_kpi`: a single row, column `total_encounters`,
> being the total encounters for Tennessee hospitals over the **same** 24-month window as
> `monthly_encounters`. Then rebuild the lesson-1 report and add, above the table, a headline
> reading "Total encounters (last 24 months):" and the total — formatted with thousands separators
> and taken from the `encounter_kpi` result, not typed. Save as "Clinical 2 Headline", formats
> html. Report status and parity.

**What you should see** — at the anchor the headline reads **11,376**.

**Terminal**
```
python runner/regenerate.py --report-id clinical_2_headline --as-of 2025-06-30
python runner/regenerate.py --report-id clinical_2_headline --as-of 2024-12-31
```

At 2024-12-31 the headline drops (a shorter window covers fewer months) — and the comma was placed
by the format, not by you. The lesson from the other tutorial applies here too: headline numbers
come from a dedicated single-row result like `encounter_kpi`, never plucked out of a bigger table.

---

## Lesson 3. A summary written by reasoning

**What you learn:** narrative regenerates at replay. You write the *instructions*; every replay
writes the prose fresh, about that replay's numbers.

**Paste into Copilot Chat**

> Same chat, rebuild the lesson-2 report and add, as the first element under the heading, a summary
> paragraph the SYSTEM writes at every replay — do not write any summary text yourself. It should
> draw on both results (`monthly_encounters` and `encounter_kpi`), be at most two sentences, and
> its goal is: "Summarize TN hospital encounter volume over the window: the total, the trend across
> the months, and the strongest and weakest month." Save as "Clinical 3 Reasoning", formats html.
> Report status, parity, and the reasoning steps stored in the definition.

The definition stores no sentence — only the step's goal, its inputs, and the sentence cap. The
paragraph is empty in the saved artifact, and parity does not compare narrative, so the save
passes before any prose exists. Replay at two dates:

**Terminal**
```
python runner/regenerate.py --report-id clinical_3_reasoning --as-of 2025-06-30
python runner/regenerate.py --report-id clinical_3_reasoning --as-of 2024-12-31
```

With `POC_REASONING=anthropic` the two replays read differently, each describing its own window's
numbers; without it, you get a plain deterministic sentence. Either way the prose is generated at
replay, from the stored goal, over fresh results. (Any number the sentence must state *exactly*
should also be a bound number placed in the text, so the gate still checks it.)

---

## Lesson 4. A trend chart

**What you learn:** charts are declarations. The definition stores a spec; the reviewed runtime
(`charts_v1.js`) draws it from the island at view time.

**Paste into Copilot Chat**

> Same chat. Rebuild the lesson-3 report and add, between the summary and the table, a **line
> chart** of monthly encounters — x axis the `encounter_month`, one line for `total_encounters`,
> drawn from the `monthly_encounters` result. Declare it per the chart contract; write no drawing
> code. Save as "Clinical 4 Chart", formats html. Report status, parity, and the chart entries
> stored in the definition.

The saved definition carries one entry in `rendering_spec.charts`; your artifact has no SVG and no
math.

**Terminal**
```
python runner/regenerate.py --report-id clinical_4_chart --as-of 2025-06-30
python runner/regenerate.py --report-id clinical_4_chart --as-of 2024-12-31
```

Open both in a browser: the line re-scales to each window because the runtime reads the island,
and the island is fresh. Every field the chart names is checked at **save** time against the
result's columns, so a typo fails the save immediately rather than drawing a blank chart later.

---

## Lesson 5. Encounters by facility

**What you learn:** a report can carry more than one result and more than one table. You add a
second breakdown — the same encounters, cut by facility instead of by month — which is the
building block of the dashboard in lesson 7.

**Paste into Copilot Chat**

> Same chat. Execute a new query as `encounters_by_facility`: from the `encounters` view, count
> encounters per **facility** for Tennessee hospitals over the same 24-month window, ordered by the
> count descending, and **with facility as a tiebreaker** ("order by the count descending, then by
> facility name"). Then rebuild the report so a second table below the monthly one shows Facility
> and Encounters (thousands-formatted). Save as "Clinical 5 By Facility", formats html. Report
> status, parity, and the stored SQL for both results.

**What you should see** — 15 facilities, led by **Columbia Maury (794)**, **Jackson Madison
(786)**, **Hendersonville Sumner (785)**, the rest close behind. The report now has two results and
two tables; parity checks both.

**Terminal**
```
python runner/regenerate.py --report-id clinical_5_by_facility --as-of 2025-06-30
python runner/regenerate.py --report-id clinical_5_by_facility --as-of 2024-12-31
```

Open in a browser: both tables shift to each replay's window.

Two things worth knowing. The tiebreaker is not optional: whenever a table is ordered by a *count*
(a computed value), add a unique text column to break ties — the parity gate compares rows
positionally, and without it a save can pass today and fail tomorrow on a field you never touched.
And the `encounters` view carries columns the platform *derived* for you — `time_zone`, for
instance (the TN hospitals split **America/Chicago 7,659** / **America/New_York 3,717**) — so
"encounters by time zone" is just as easy a one-sentence ask, with no extra work on your part.

---

## Lesson 6. A thesis with a watch

**What you learn:** not all prose should regenerate. Analysis is recomputed (lesson 3); judgment
is preserved — verbatim, dated, and watching the number that could invalidate it.

**Paste into Copilot Chat**

> Same chat. Add to the lesson-5 report, below the tables, this sentence as an EDITORIAL block — my
> judgment, replayed word-for-word, never rewritten: "Thesis: TN hospital encounter volume is
> stable and broad-based, with no single facility carrying the network." Mark it authored as of
> 2025-06-30, and give it a watch that flags it whenever `encounter_kpi`'s `total_encounters` falls
> below 9,000. Save as "Clinical 6 Watch", formats html. Report status, parity, and the editorial
> blocks stored.

**Terminal**
```
python runner/regenerate.py --report-id clinical_6_watch --as-of 2025-06-30   # total 11,376
python runner/regenerate.py --report-id clinical_6_watch --as-of 2024-12-31   # shorter window, fewer
```

At the anchor the total (11,376) is above 9,000, so the thesis appears verbatim, stamped, with **no
banner**. Replay at a date whose 24-month window is short enough that the total dips below 9,000 and
the same thesis appears — stamped identically — now **with an amber staleness banner**. The machine
never rewrites your judgment; it only tells the reader when the world moved under it. Pick a
threshold the data can actually cross: eyeball a couple of replays first to choose one.

---

## Lesson 7. A two-tab clinical dashboard

**What you learn:** nothing new — composition is more of the same declarations under a theme and a
layout.

**Paste into Copilot Chat**

> Same chat. Rebuild everything as a two-tab dashboard using theme "market-story-v1" and layout
> "tabbed-dashboard", tabs named Trend and Facilities. On Trend: the reasoning summary, the total
> headline, the monthly line chart, and the thesis with its watch. On Facilities: the
> encounters-by-facility table. Save as "Clinical 7 Dashboard", formats html. Report status,
> parity, warnings.

**What you should see**

```
status: registered   parity: passed, attempt 1
warnings: ["layout 'tabbed-dashboard' renders HTML only; markdown output skipped"]
```

The theme and tabs came from the server's template library; your artifact declared section
membership and nothing else.

**Terminal**
```
python runner/regenerate.py --report-id clinical_7_dashboard --as-of 2025-06-30
python runner/regenerate.py --report-id clinical_7_dashboard --as-of 2024-12-31
```

Open both in a browser and walk both tabs: line redrawn, tables shifted, summary rewritten, thesis
constant, banner only when the total crosses the watch.

---

## Lesson 8. The same dashboard, built free-form

**What you learn:** the contract can be inferred. You build lesson 7's report with **zero** contract
markup and let save-time extraction recover the structure, confirming its judgment calls.

**Paste into Copilot Chat**

> Start a NEW chat. From the `encounters` view, re-run the three queries from the earlier lessons
> with `execute_sql` — `monthly_encounters` (monthly counts, TN hospitals, last 24 months ending
> 2025-06-30), `encounter_kpi` (the single-row total for that window), and `encounters_by_facility`
> (per-facility counts, tiebroken by facility). Then build a self-contained web page WITHOUT the
> report markup contract — your own layout and your own
> chart drawing, as if the contract did not exist: a title, the total headline, a line chart of the
> monthly series, the monthly table, the facility table, one short analytical paragraph about the
> trend, and the thesis sentence. Every number and label must be a value `execute_sql` returned,
> used verbatim — never compute or round in the page. Write it to `reports/_clinical8.html`. Then
> call `save_report_definition` as "Clinical 8 Free Form" with that file as content, formats html.
> Show me the full response.

With the LLM extractor on (`POC_DISTILLER=anthropic`) expect `needs_structure_confirmation`: the
constants matched to results by fingerprint (facts — the rows are equal, nothing to confirm), a
chart spec recovered from your drawing code, the paragraph proposed as analytical with a goal, the
thesis proposed as editorial, and a single-use token. Then confirm, adding the one thing the
extractor cannot invent:

**Paste into Copilot Chat**

> Save again, accepting the whole proposal (use the confirmation token), with one addition: the
> thesis block is editorial judgment, authored as of 2025-06-30, with a watch that flags it whenever
> `encounter_kpi`'s `total_encounters` falls below 9,000. Report status, parity, and the editorial
> blocks stored.

**What you should see**

```
status: registered   parity: passed, attempt 1
editorial_blocks: [{block_id, authored_as_of: "2025-06-30", watch: total_encounters < 9000}]
```

**Terminal**
```
python runner/regenerate.py --report-id clinical_8_free_form --as-of 2025-06-30
python runner/regenerate.py --report-id clinical_8_free_form --as-of 2024-12-31
```

Open both in a browser. The output is indistinguishable from lesson 7's — same sliding window, same
rewritten analysis, same watched thesis — because it is the same kind of definition. However a
report is authored, exactly one thing registers, and only by proving itself.

---

## Where you are now

You have built a full clinical report the same way as the market story: named results and islands
(1), bound numbers (2), replay-time reasoning (3), declarative charts (4), a second result and
table (5), the editorial tier and watches (6), composition (7), and free-form extraction (8) — all
over one plain-English `encounters` view.

Two habits to carry forward: **anchor a window on the anchor date (2025-06-30) at both edges** so
it tokenizes and slides on replay, and **always give a count-ordered table a text tiebreaker**.
Everything else you said in plain English.

Under the hood, the `encounters` view (defined in `data/seed.py`, `_create_hca_clinical`) sits on
the raw clinical tables — `clinical_core_silver.encounter`, the facility master, and the
`enterprise_ontology_gold.facility_master_site` view — which it joins, casts, filters, and
de-duplicates for you. The DuckDB translation of the original BigQuery query is in
`data/queries/tn_monthly_encounters.sql`. Point an advanced audience at those; a first-time
audience never needs them.
