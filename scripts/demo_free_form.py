"""Capture a deliberately contract-free report and let the server extract structure.

Where `demo_market_story.py` builds a v2-contract artifact -- islands, data-value
spans, declarative charts -- this script builds the page the way a person would:
JavaScript constants, a hand-rolled inline SVG chart, KPI numbers as plain
`<strong>` text, and prose. No `data-*` attributes anywhere.

    python scripts/demo_free_form.py

`save_report_definition` fingerprints every number against what `execute_sql`
returned, proposes a normalization plan, and (when the plan rests on an
inference) asks for confirmation before registering anything.

The page carries one constant the queries do not return: `RACE_GAP`, holding the
gap as a percentage of the leader. Two honest paths follow from that:

* **Deterministic (POC_DISTILLER unset).** A tenth `execute_sql` pre-derives
  `race_gap_pct`, so the constant fingerprints cleanly, the plan holds no
  inference, and the report registers in a single call with no network.
* **LLM (POC_DISTILLER=anthropic).** The tenth query is not run. The constant is
  unmatched, and the extractor must propose a derived query that reproduces it.
  The server executes that query and checks it really does before accepting it.

The script prints which path it took, so the demo never takes credit for work the
model did not do.

Runs happily alongside the MCP server: the warehouse is opened read-only.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import market_queries as queries  # noqa: E402
from server import tools  # noqa: E402
from server.db import get_connection, get_meta_connection  # noqa: E402

REPORT_NAME = "Las Vegas Free Form"
TITLE = "Las Vegas Market Story (hand-built)"


def _using_llm() -> bool:
    return os.environ.get("POC_DISTILLER", "").lower() in ("anthropic", "llm", "claude")


# The two paths must not share a conversation. The tool-call log is lineage, and a
# deterministic run leaves `race_gap_pct` in it -- against which the LLM run's
# RACE_GAP constant would then "fingerprint", letting the demo take credit for a
# derivation the model never made.
CID = "las-vegas-free-form-llm" if _using_llm() else "las-vegas-free-form"

# The gap as a share of the leader's volume. No query in market_queries returns
# this, which is exactly the point: it is the one thing extraction must derive.
GAP_PCT_SQL = f"""
WITH quarterly AS (
  SELECT period_quarter,
         SUM(cases) FILTER (WHERE is_hca) AS hca,
         SUM(cases) FILTER (WHERE health_system = 'Universal Health Services') AS uhs
  FROM marketshare_volume
  WHERE period_quarter >= {queries.RACE_START} AND period_quarter < {queries.UPPER}
  GROUP BY period_quarter
)
SELECT 'Q' || quarter(period_quarter) || '''' || strftime(period_quarter, '%y') AS qtr,
       ROUND((uhs - hca) * 100.0 / uhs, 2) AS gap_pct
FROM quarterly
ORDER BY period_quarter
""".strip()


# --- building the contract-free page ---------------------------------------


def _js_value(value) -> str:
    return json.dumps(value)


def _js_rows(rows, columns) -> str:
    body = ",\n  ".join(
        "{" + ", ".join(f"{c}: {_js_value(r[c])}" for c in columns) + "}" for r in rows
    )
    return f"[\n  {body},\n]"  # a trailing comma, as hand-written JS tends to have


def _js_grouped(rows, key: str, columns) -> str:
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(row[key], []).append(row)
    parts = []
    for group_key, group_rows in groups.items():
        inner = ", ".join(
            "{" + ", ".join(f"{c}: {_js_value(r[c])}" for c in columns) + "}"
            for r in group_rows
        )
        parts.append(f"  {_js_value(group_key)}: [{inner}]")
    return "{\n" + ",\n".join(parts) + ",\n}"


_SVG_CHART = """
function drawRace(rows) {
  var w = 720, h = 260, pad = 40;
  var max = 0;
  rows.forEach(function (d) { if (d.uhs > max) { max = d.uhs; } });
  var step = (w - pad * 2) / (rows.length - 1);
  var pts = function (field) {
    return rows.map(function (d, i) {
      var x = pad + i * step;
      var y = h - pad - (d[field] / max) * (h - pad * 2);
      return x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');
  };
  var svg = document.getElementById('race-chart');
  if (!svg) { return; }
  svg.innerHTML =
    '<polyline fill="none" stroke="#092240" stroke-width="2" points="' + pts('uhs') + '"/>' +
    '<polyline fill="none" stroke="#E75925" stroke-width="2" points="' + pts('hca') + '"/>';
}
"""


def _page(race_rows, esl_rows, gap_rows, kpi) -> str:
    """The KPI strip is deliberately mixed.

    `esl_gainers` and `esl_total` appear exactly once across all nine results, so
    they bind to a `data-value` span with no guesswork. `hca_share_pct` (35.3)
    also appears in `competitor_yoy` and `esl_summary`; picking one of the three
    would be a guess, so extraction reports it as ambiguous and leaves the number
    frozen. That is the design working, not failing.
    """
    race_js = _js_rows(race_rows, ["qtr", "hca", "uhs", "gap", "gap_trend"])
    esl_js = _js_grouped(esl_rows, "esl", ["qtr", "share_pct"])
    gap_js = _js_rows(gap_rows, ["qtr", "gap_pct"])

    return f"""<div class="report">
<style>
  .report {{ font-family: system-ui, sans-serif; max-width: 960px; }}
  .kpi-strip {{ display: flex; gap: 16px; }}
  .kpi-card {{ padding: 12px; border: 1px solid #ddd; border-radius: 8px; }}
  .kpi-label {{ display: block; font-size: 12px; color: #666; }}
  .kpi-card strong {{ font-size: 28px; }}
</style>

<h1>The Race to #1</h1>

<div class="kpi-strip">
  <div class="kpi-card"><span class="kpi-label">Service lines gaining share</span>
    <strong>{kpi['esl_gainers']}</strong></div>
  <div class="kpi-card"><span class="kpi-label">Service lines tracked</span>
    <strong>{kpi['esl_total']}</strong></div>
  <div class="kpi-card"><span class="kpi-label">HCA share</span>
    <strong>{kpi['hca_share_pct']}%</strong></div>
</div>

<svg id="race-chart" width="720" height="260"></svg>

<p>Across the window shown, the gap to the market leader has closed in every
single quarter without interruption, and the two systems now sit within a few
hundred cases of one another.</p>

<p>Orthopedics is the exception that deserves attention. It is the only surgical
line losing share, and it is doing so in a market that is growing, which turns a
relative decline into an absolute one.</p>

<p>Thesis: HCA can take the market outright within four quarters, but only by
arresting the orthopedic slide before it consumes the surgical gains made
everywhere else.</p>

<script>
const RACE = {race_js};

const ESL_QTR = {esl_js};

const RACE_GAP = {gap_js};
{_SVG_CHART}
drawRace(RACE);
</script>
</div>"""


# --- driving the capture path ----------------------------------------------


def _fresh_conversation() -> None:
    """Clear this demo conversation's lineage so each run starts as a new session.

    Without it, a previous run's `save_derive` of the gap query stays in the log,
    and the LLM path's RACE_GAP constant fingerprints against it -- letting the
    demo claim a derivation the model never had to make.
    """
    meta = get_meta_connection()
    deleted = meta.execute(
        "DELETE FROM tool_call_log WHERE conversation_id = ?", (CID,)
    ).rowcount
    meta.execute("DELETE FROM extraction_plans WHERE conversation_id = ?", (CID,))
    meta.commit()
    if deleted:
        print(f"Cleared {deleted} logged calls from a previous run of {CID!r}.")


def _run_queries() -> dict:
    print("Running the market-share queries...")
    results = {}
    for name, sql in queries.QUERIES:
        result = tools.execute_sql(CID, sql, name)
        if "error" in result:
            raise SystemExit(f"query {name} failed: {result['error']}")
        print(f"  query {name}: {result['row_count']} rows")
        results[name] = result
    return results


def _gap_rows(use_llm: bool) -> list[dict]:
    """The gap-percentage rows the page displays, and how they got there."""
    if use_llm:
        # The page's author computed these in the browser. We read them straight
        # from the warehouse without logging a call, so extraction faces the same
        # thing it would face in real life: numbers with no lineage.
        con = get_connection()
        rows = con.execute(GAP_PCT_SQL).fetchall()
        columns = [d[0] for d in con.description]
        print("  path: LLM -- RACE_GAP has no lineage; the extractor must derive it")
        return [dict(zip(columns, row)) for row in rows]

    result = tools.execute_sql(CID, GAP_PCT_SQL, "race_gap_pct")
    if "error" in result:
        raise SystemExit(f"gap query failed: {result['error']}")
    print(f"  query race_gap_pct: {result['row_count']} rows (tenth query, pre-derived)")
    print("  path: deterministic -- RACE_GAP fingerprints against race_gap_pct")
    return result["rows"]


def _print_extraction(extraction: dict) -> None:
    print("\nThe server proposes this normalization:")
    for island in extraction["matched_islands"]:
        print(f"  island  {island['result_name']:<18} <- {island['source']}  (fingerprinted)")
    for island in extraction["proposed_islands"]:
        print(f"  island  {island['result_name']:<18} <- {island['source']}  (INFERRED)")
    for derived in extraction["derived_queries"]:
        print(f"  derive  {derived['result_name']}  covers {derived['covers']}")
        for line in derived["sql"].splitlines():
            print(f"            {line}")
    for block in extraction["narrative"]:
        print(f"  prose   {block['block_id']} -> {block['tier']}: {block['excerpt'][:60]}...")
    for item in extraction["unmatched"]:
        print(f"  UNMATCHED  {item}")


def main() -> dict:
    use_llm = _using_llm()
    _fresh_conversation()
    results = _run_queries()
    gap_rows = _gap_rows(use_llm)

    html = _page(
        results["race_quarters"]["rows"],
        results["esl_quarters"]["rows"],
        gap_rows,
        results["kpi_summary"]["rows"][0],
    )
    print(f"  artifact: {len(html)} characters, zero data-* attributes")

    artifact = {"format": "html", "title": TITLE, "content": html}
    transcript = [
        {"role": "user", "content": "Show me the Las Vegas race to #1."},
        {"role": "assistant", "content": "Here is the market story."},
    ]

    print("\n--- save_report_definition (first call) ---")
    result = tools.save_report_definition(
        CID, REPORT_NAME, transcript, artifact, queries.TEMPORAL_CONFIRMATIONS
    )

    if result["status"] == "needs_structure_confirmation":
        _print_extraction(result["extraction"])
        print("\n--- save_report_definition (second call, accept_all) ---")
        result = tools.save_report_definition(
            CID,
            REPORT_NAME,
            transcript,
            artifact,
            queries.TEMPORAL_CONFIRMATIONS,
            [{"token": result["confirmation_token"], "accept_all": True}],
        )
    else:
        print("  no inferences in the plan; registered without a confirmation round-trip")

    print(f"\nstatus: {result['status']}")
    if result.get("parity"):
        print(f"parity: {result['parity']}")
    for warning in result.get("warnings", []):
        print(f"  warning: {warning}")
    for section in result.get("unreplayable_sections", []):
        print(f"  unreplayable: {section}")

    if result["status"] == "registered":
        print(f"\nREPORT_ID={result['report_id']}")
        print("Replay it with:")
        print(
            f"  python runner/regenerate.py --report-id {result['report_id']} "
            "--as-of 2025-03-31"
        )
    return result


if __name__ == "__main__":
    main()
