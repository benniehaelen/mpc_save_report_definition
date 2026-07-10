"""End-to-end test: a scripted session against the tools in-process (no MCP
transport), then regeneration as of a different date.

Proves: dead-end queries drop out, stored SQL is tokenized, metric bindings and
reasoning steps are distilled, the report registers, and regenerating as of a
different date keeps the structure but changes both the data and the narrative.
"""

from __future__ import annotations

from runner import render
from server import parity, reasoning, registry, tools
from server.db import ANCHOR_DATE, get_connection, get_meta_connection

CID = "e2e-session"

_ADMISSIONS = (
    "SELECT f.division, COUNT(*) AS admissions "
    "FROM admissions a JOIN facilities f ON a.facility_id = f.facility_id "
    "WHERE a.admit_date >= DATE '2025-06-01' AND a.admit_date < DATE '2025-07-01' "
    "GROUP BY f.division ORDER BY f.division"
)
_CENSUS = (
    "SELECT f.facility_name, ROUND(AVG(c.midnight_census), 1) AS avg_census, "
    "ROUND(AVG(c.midnight_census * 1.0 / f.bed_count), 4) AS occupancy_rate "
    "FROM daily_census c JOIN facilities f ON c.facility_id = f.facility_id "
    "WHERE c.census_date >= DATE '2025-06-01' AND c.census_date < DATE '2025-07-01' "
    "GROUP BY f.facility_name ORDER BY f.facility_name"
)
_OVERALL = (
    "SELECT ROUND(AVG(c.midnight_census * 1.0 / f.bed_count), 4) AS occupancy_rate "
    "FROM daily_census c JOIN facilities f ON c.facility_id = f.facility_id "
    "WHERE c.census_date >= DATE '2025-06-01' AND c.census_date < DATE '2025-07-01'"
)


def _run_session():
    tools.execute_sql(CID, "SELECT COUNT(*) AS n FROM admissions", "scratch")
    adm = tools.execute_sql(CID, _ADMISSIONS, "admissions_by_division")
    cen = tools.execute_sql(CID, _CENSUS, "census_by_facility")
    ovr = tools.execute_sql(CID, _OVERALL, "overall_occupancy")

    content = (
        "<h1>Division Admissions and Census</h1>"
        + render.build_table_html(
            "admissions_by_division", adm["columns"], adm["rows"]
        )
        + render.build_table_html("census_by_facility", cen["columns"], cen["rows"])
        + "<p class='headline'>Occupancy: "
        + render.build_value_span(
            "overall_occupancy", "occupancy_rate", ovr["rows"][0]["occupancy_rate"]
        )
        + "</p>"
        + render.build_reasoning_para(
            "occ_summary", "census_by_facility", "occupancy_rate", "max"
        )
    )
    return {
        "format": "html",
        "title": "Division Admissions and Census",
        "content": content,
        "formats": ["html", "md"],
    }


def test_end_to_end_save_and_regenerate():
    artifact = _run_session()
    result = tools.save_report_definition(
        CID,
        "Division Admissions and Census",
        transcript=[{"role": "user", "content": "admissions by division"}],
        final_artifact=artifact,
    )

    assert result["status"] == "registered", result
    assert result["parity"]["passed"]
    report_id = result["report_id"]

    con = get_connection()
    definition = registry.get(get_meta_connection(), report_id)

    # Dead-end query dropped out.
    names = [q["result_name"] for q in definition["parameterized_sql"]]
    assert "scratch" not in names
    assert set(names) == {
        "admissions_by_division",
        "census_by_facility",
        "overall_occupancy",
    }

    # Stored SQL is tokenized, not literal.
    for query in definition["parameterized_sql"]:
        assert "__REPORT_DATE__" in query["sql"]
        assert "DATE '2025-06-01'" not in query["sql"]

    # Metric bindings were distilled and validated (no binding warnings).
    metrics = {b["metric_id"] for b in definition["metric_bindings"] if b["metric_id"]}
    value_sets = {b["value_set"] for b in definition["metric_bindings"] if b["value_set"]}
    assert "occupancy_rate" in metrics
    assert "admissions" in metrics
    assert "divisions" in value_sets
    assert not any("binding validation" in w for w in definition["warnings"])

    # Reasoning steps were distilled.
    step_ids = {s["step_id"] for s in definition["reasoning_steps"]}
    assert step_ids == {"occ_summary"}
    assert definition["rendering_spec"]["formats"] == ["html", "md"]

    engine = reasoning.get_engine()

    def _render(as_of: str):
        results = {
            q["result_name"]: parity.run_named_query(con, q["sql"], as_of)
            for q in definition["parameterized_sql"]
        }
        narratives = engine.run(definition["reasoning_steps"], results)
        html = render.render_html(definition, results, narratives)
        md = render.render_markdown(definition, results, narratives)
        return parity._extract_tables(html), narratives, md

    anchor_tables, anchor_narr, anchor_md = _render(ANCHOR_DATE)
    other_tables, other_narr, _ = _render("2025-05-15")

    # Regenerating at the anchor reproduces the original data.
    assert anchor_tables == parity._extract_tables(artifact["content"])

    # A different date keeps the structure but changes data and narrative.
    assert set(other_tables) == set(anchor_tables)
    assert other_tables["admissions_by_division"] != anchor_tables[
        "admissions_by_division"
    ]
    assert other_narr["occ_summary"] != anchor_narr["occ_summary"]

    # Markdown comes from the same rendering spec.
    assert anchor_md.startswith("# Division Admissions and Census")
    assert "| division | admissions |" in anchor_md
