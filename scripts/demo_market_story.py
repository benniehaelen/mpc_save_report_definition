"""Capture the Las Vegas market story end to end, without a Copilot in the loop.

Mirrors `scripts/demo_session.py`, but for the v2 artifact contract: nine
quarter-windowed queries, JSON data islands, twelve declarative charts, four
runtime-bound tables, three goal-directed reasoning steps, two editorial blocks,
and a six-tab layout.

    python scripts/demo_market_story.py

Runs happily alongside the MCP server: the warehouse is opened read-only and
writes go to the SQLite metadata store.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runner import render  # noqa: E402
from scripts import market_queries as queries  # noqa: E402
from server import tools  # noqa: E402

CID = "las-vegas-demo"

# slugify() turns this into `las_vegas_race_to_1`, the id the runner replays.
REPORT_NAME = "Las Vegas Race to #1"
TITLE = "Las Vegas Market — The Race to #1"

SECTIONS = [
    {"id": "story", "label": "Executive Story"},
    {"id": "race", "label": "The Race to #1"},
    {"id": "category", "label": "Category Trends"},
    {"id": "esl", "label": "Surgical Deep-Dive"},
    {"id": "trajectory", "label": "ESL Trajectories"},
    {"id": "strategy", "label": "Strategic Assessment"},
]

HCA_ORANGE = "#E75925"
HCA_NAVY = "#092240"
GREEN = "#16a34a"
RED = "#dc2626"

_FILTERS = {
    "thousands": render.do_thousands,
    "signed": render.do_signed,
    "pct": render.do_pct,
    "pp": render.do_pp,
}


def _fmt(value, *chain) -> str:
    """Apply the same filter chain the template will, so the artifact matches."""
    for step in chain:
        name, *args = step if isinstance(step, tuple) else (step,)
        value = _FILTERS[name](value, *args)
    return str(value)


def _section(section_id: str, body: str) -> str:
    return f'<div data-section="{section_id}">{body}</div>'


def _kpi(label: str, value_html: str, detail_html: str = "", hero: bool = False) -> str:
    classes = "kpi-card hero" if hero else "kpi-card"
    detail = f'<div class="detail">{detail_html}</div>' if detail_html else ""
    return (
        f'<div class="{classes}">'
        f'<div class="kpi-label">{label}</div>'
        f'<div class="value">{value_html}</div>{detail}</div>'
    )


def _run_queries() -> dict[str, dict]:
    results: dict[str, dict] = {}
    for name, sql in queries.QUERIES:
        result = tools.execute_sql(CID, sql, name)
        if "error" in result:
            raise SystemExit(f"query {name} failed: {result['error']}")
        print(f"  query {name}: {result['row_count']} rows")
        results[name] = result
    return results


def _kpi_strip(kpi: dict) -> str:
    row = kpi["rows"][0]
    cards = [
        _kpi(
            "Gap to #1",
            render.build_value_span_v2(
                "kpi_summary[0].gap_now | thousands", _fmt(row["gap_now"], "thousands")
            ),
            "cases — down from "
            + render.build_value_span_v2(
                "kpi_summary[0].gap_then | thousands",
                _fmt(row["gap_then"], "thousands"),
            )
            + " in "
            + render.build_value_span_v2("kpi_summary[0].first_qtr", row["first_qtr"]),
            hero=True,
        ),
        _kpi(
            "Overall share",
            render.build_value_span_v2(
                "kpi_summary[0].hca_share_pct | pct(1)",
                _fmt(row["hca_share_pct"], ("pct", 1)),
            ),
            "#2 behind UHS ("
            + render.build_value_span_v2(
                "kpi_summary[0].uhs_share_pct | pct(1)",
                _fmt(row["uhs_share_pct"], ("pct", 1)),
            )
            + ")",
        ),
        _kpi(
            "Service lines gaining share",
            render.build_value_span_v2(
                "kpi_summary[0].esl_gainers", row["esl_gainers"], css_class="growth"
            )
            + " of "
            + render.build_value_span_v2("kpi_summary[0].esl_total", row["esl_total"]),
            render.build_value_span_v2(
                "kpi_summary[0].surgical_vol_change | signed | thousands",
                _fmt(row["surgical_vol_change"], "signed", "thousands"),
            )
            + " net HCA surgical cases",
        ),
        _kpi(
            "ER share stability",
            render.build_value_span_v2(
                "kpi_summary[0].er_share_pct | pct(1)",
                _fmt(row["er_share_pct"], ("pct", 1)),
            ),
            "flat across eight quarters",
        ),
        _kpi(
            "Orthopedics risk",
            render.build_value_span_v2(
                "kpi_summary[0].ortho_change_pp | pp",
                _fmt(row["ortho_change_pp"], "pp"),
                style="sign",
            ),
            "share lost in a growing market",
        ),
    ]
    return f'<div class="kpi-strip">{"".join(cards)}</div>'


def _story_section() -> str:
    thesis = render.build_editorial_block(
        "thesis",
        "<strong>Thesis: HCA can overtake UHS for #1.</strong> The gap has "
        "narrowed every quarter for four years, driven by surgical share capture "
        "across a broad set of service lines rather than one program. The breadth "
        "is what makes the trajectory credible: no single line has to keep "
        "outperforming for the crossover to happen.",
        "2025-06-30",
        # Fires once the gap closes past 800 cases -- at which point the thesis
        # is no longer a forecast and the prose needs rewriting.
        watch="kpi_summary[0].gap_now < 800",
    )
    reasoning = render.build_reasoning_block(
        "race_story",
        "Describe how the gap to the market leader moved across these quarters, "
        "and where it stands now.",
        ["race_quarters", "kpi_summary"],
        max_sentences=3,
    )
    return _section(
        "story",
        f'<div class="insight-box">{thesis}</div>'
        f'<div class="story-section"><h4>'
        f'<span class="section-num">1</span>The race, in one paragraph</h4>'
        f"{reasoning}</div>",
    )


def _race_section() -> str:
    race_chart = render.build_chart_div(
        {
            "type": "line",
            "result": "race_quarters",
            "x": "qtr",
            "series": [
                {"field": "uhs", "label": "UHS", "color": HCA_NAVY},
                {"field": "hca", "label": "HCA", "color": HCA_ORANGE},
            ],
            "width": 700,
            "height": 300,
        },
        "raceChart",
    )
    gap_chart = render.build_chart_div(
        {
            "type": "line",
            "result": "race_quarters",
            "x": "qtr",
            "series": [{"field": "gap", "label": "Gap", "color": HCA_ORANGE}],
            "width": 700,
            "height": 260,
        },
        "gapChart",
    )
    competitor_chart = render.build_chart_div(
        {
            "type": "bar",
            "result": "competitor_yoy",
            "label_field": "health_system",
            "value_field": "current_cases",
            "display_field": "share_display",
            "color": HCA_NAVY,
            "highlight": {
                "field": "health_system",
                "value": "HCA Healthcare",
                "color": HCA_ORANGE,
            },
            "height": 240,
        },
        "competitorChart",
    )
    gap_table = render.build_bound_table(
        "race_quarters",
        [
            {"field": "qtr", "header": "Quarter"},
            {"field": "hca", "header": "HCA", "filters": [["thousands", []]]},
            {"field": "uhs", "header": "UHS", "filters": [["thousands", []]]},
            {"field": "gap", "header": "Gap", "filters": [["thousands", []]]},
            {
                "field": "gap_trend",
                "header": "Trend",
                "filters": [["signed", []], ["thousands", []]],
                "style": "sign",
            },
        ],
    )
    yoy_table = render.build_bound_table(
        "competitor_yoy",
        [
            {"field": "health_system", "header": "Health system"},
            {"field": "prior_cases", "header": "Prior year", "filters": [["thousands", []]]},
            {"field": "current_cases", "header": "Current year", "filters": [["thousands", []]]},
            {
                "field": "vol_change",
                "header": "Change",
                "filters": [["signed", []], ["thousands", []]],
                "style": "sign",
            },
            {"field": "share_pct", "header": "Share", "filters": [["pct", [1]]]},
        ],
    )
    return _section(
        "race",
        f'<div class="card"><h3>HCA vs UHS, quarterly inpatient cases</h3>{race_chart}</div>'
        f'<div class="card"><h3>The gap, quarter by quarter</h3>{gap_chart}{gap_table}</div>'
        f'<div class="card"><h3>Market position</h3>{competitor_chart}{yoy_table}</div>',
    )


def _category_section() -> str:
    chart = render.build_chart_div(
        {
            "type": "line",
            "result": "category_share_quarters",
            "x": "qtr",
            "series": [
                {"field": "er", "label": "ER", "color": HCA_ORANGE},
                {"field": "surgical", "label": "Surgical", "color": GREEN},
                {"field": "medical", "label": "Medical", "color": HCA_NAVY},
            ],
            "width": 650,
            "height": 300,
            "suffix": "%",
        },
        "categoryChart",
    )
    table = render.build_bound_table(
        "category_detail",
        [
            {"field": "qtr", "header": "Quarter"},
            {"field": "category", "header": "Category"},
            {"field": "market_cases", "header": "Market", "filters": [["thousands", []]]},
            {"field": "hca_cases", "header": "HCA", "filters": [["thousands", []]]},
            {"field": "share_pct", "header": "Share", "filters": [["pct", [1]]]},
        ],
    )
    reasoning = render.build_reasoning_block(
        "category_story",
        "Explain how HCA's share of ER, surgical, and medical volume has shifted.",
        ["category_share_quarters", "category_share_change"],
        max_sentences=3,
    )
    return _section(
        "category",
        f'<div class="card"><h3>HCA share by category</h3>{chart}</div>'
        f'<div class="note-box">{reasoning}</div>'
        f'<div class="card"><h3>Category detail</h3>{table}</div>',
    )


def _esl_section() -> str:
    share_chart = render.build_chart_div(
        {
            "type": "diverging_bar",
            "result": "esl_share_change",
            "label_field": "esl",
            "value_field": "share_change_pp",
            "display_field": "share_display",
            "pos_color": HCA_ORANGE,
            "neg_color": HCA_NAVY,
        },
        "eslShareChart",
    )
    vol_chart = render.build_chart_div(
        {
            "type": "diverging_bar",
            "result": "esl_summary",
            "label_field": "esl",
            "value_field": "vol_change",
            "pos_color": GREEN,
            "neg_color": RED,
        },
        "eslVolChart",
    )
    table = render.build_bound_table(
        "esl_summary",
        [
            {"field": "esl", "header": "Service line"},
            {"field": "prior_market", "header": "Prior market", "filters": [["thousands", []]]},
            {"field": "prior_share", "header": "Prior share", "filters": [["pct", [1]]]},
            {"field": "current_market", "header": "Market", "filters": [["thousands", []]]},
            {"field": "current_share", "header": "Share", "filters": [["pct", [1]]]},
            {
                "field": "share_change_pp",
                "header": "Share change",
                "filters": [["signed", []], ["pp", []]],
                "style": "sign",
            },
            {
                "field": "vol_change",
                "header": "Volume change",
                "filters": [["signed", []], ["thousands", []]],
                "style": "sign",
            },
        ],
    )
    reasoning = render.build_reasoning_block(
        "esl_story",
        "Identify where HCA is gaining and losing surgical share, and call out "
        "orthopedics specifically.",
        ["esl_summary", "esl_quarters[esl='ORTHOPEDICS']"],
        max_sentences=3,
    )
    return _section(
        "esl",
        f'<div class="note-box">{reasoning}</div>'
        f'<div class="two-col">'
        f'<div class="card"><h3>Share change by service line</h3>{share_chart}</div>'
        f'<div class="card"><h3>Volume change by service line</h3>{vol_chart}</div>'
        f"</div>"
        f'<div class="card"><h3>Service-line detail</h3>{table}</div>',
    )


def _trajectory_section() -> str:
    cards = []
    for esl in queries.TRAJECTORY_ESLS:
        colour = RED if esl == "ORTHOPEDICS" else GREEN
        chart = render.build_chart_div(
            {
                "type": "line",
                "result": "esl_quarters",
                "x": "qtr",
                "series": [{"field": "share_pct", "color": colour}],
                "filter": {"esl": esl},
                "width": 460,
                "height": 180,
                "suffix": "%",
            },
            f"eslTrend{esl.replace(' ', '').replace('(', '').replace(')', '')}",
        )
        cards.append(
            f'<div class="card"><h3 style="color:{colour};">{esl}</h3>{chart}</div>'
        )
    return _section(
        "trajectory",
        '<div class="note-box"><strong>Quarterly share trajectories</strong> for six '
        "service lines, over the last eight complete quarters.</div>"
        f'<div class="two-col">{"".join(cards)}</div>',
    )


def _strategy_section() -> str:
    watchlist = render.build_editorial_block(
        "watchlist",
        "<strong>Watchlist.</strong> Orthopedics is the one major service line "
        "losing share while its market grows, which points at outpatient and ASC "
        "migration rather than competitive loss. Medical share compression traces "
        "almost entirely to a single new entrant and should stabilise once it "
        "matures.",
        "2025-06-30",
    )
    return _section(
        "strategy",
        f'<div class="card"><h3>'
        f'<span class="section-num">2</span>Strategic assessment</h3>'
        f'<div class="insight-box">{watchlist}</div></div>',
    )


def build_artifact(results: dict[str, dict]) -> dict:
    islands = "".join(
        render.build_island(name, results[name]) for name, _sql in queries.QUERIES
    )
    content = (
        islands
        + _kpi_strip(results["kpi_summary"])
        + render.build_tabs(SECTIONS)
        + _story_section()
        + _race_section()
        + _category_section()
        + _esl_section()
        + _trajectory_section()
        + _strategy_section()
    )
    return {
        "format": "html",
        "title": TITLE,
        "content": content,
        "formats": ["html", "md"],  # md is dropped: a tabbed layout is HTML only
        "layout": "tabbed-dashboard",
        "theme": "market-story-v1",
    }


def main() -> dict:
    print("Running the market-share queries...")
    results = _run_queries()

    artifact = build_artifact(results)
    print(f"  artifact: {len(artifact['content'])} characters")

    transcript = [
        {"role": "user", "content": "How close is HCA to overtaking UHS in Las Vegas?"},
        {
            "role": "assistant",
            "content": "The gap has narrowed every quarter for four years, driven by "
            "surgical share gains in 12 of 17 service lines.",
        },
    ]
    result = tools.save_report_definition(
        CID,
        REPORT_NAME,
        transcript=transcript,
        final_artifact=artifact,
        temporal_confirmations=queries.TEMPORAL_CONFIRMATIONS,
    )
    print(json.dumps(result, indent=2, default=str))
    if result["status"] == "registered":
        print(f"\nREPORT_ID={result['report_id']}")
        print(
            "Replay it with:\n"
            f"  python runner/regenerate.py --report-id {result['report_id']} "
            "--as-of 2025-06-30"
        )
    return result


if __name__ == "__main__":
    main()
