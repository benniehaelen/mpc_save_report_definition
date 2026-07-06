"""End-to-end test: a scripted session against the tools in-process (no MCP
transport), then regeneration as of a different date.

Proves: dead-end queries drop out, stored SQL is tokenized, the report registers,
and regenerating as of a different date keeps the structure but changes the data.
"""

from __future__ import annotations

from runner import render
from server import call_log, parity, registry, tools
from server.db import ANCHOR_DATE, get_connection

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
    # A throwaway query that is never referenced by the artifact.
    tools.execute_sql(CID, "SELECT COUNT(*) AS n FROM admissions", "scratch")
    adm = tools.execute_sql(CID, _ADMISSIONS, "admissions_by_division")
    cen = tools.execute_sql(CID, _CENSUS, "census_by_facility")
    ovr = tools.execute_sql(CID, _OVERALL, "overall_occupancy")

    content = (
        "<h1>Division Admissions and Census</h1>"
        + render.build_table_html(
            "admissions_by_division", adm["columns"], adm["rows"]
        )
        + render.build_table_html(
            "census_by_facility", cen["columns"], cen["rows"]
        )
        + "<p class='headline'>Occupancy: "
        + render.build_value_span(
            "overall_occupancy", "occupancy_rate", ovr["rows"][0]["occupancy_rate"]
        )
        + "</p>"
    )
    return {"format": "html", "title": "Division Admissions and Census", "content": content}


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
    definition = registry.get(con, report_id)

    # Dead-end query dropped out.
    names = [q["result_name"] for q in definition["queries"]]
    assert "scratch" not in names
    assert set(names) == {
        "admissions_by_division",
        "census_by_facility",
        "overall_occupancy",
    }

    # Stored SQL is tokenized, not literal.
    for query in definition["queries"]:
        assert "__REPORT_DATE__" in query["sql"]
        assert "DATE '2025-06-01'" not in query["sql"]

    # Registered and listable.
    assert any(r["report_id"] == report_id for r in registry.list_all(con))

    # Regenerate at the anchor: values match the original session.
    at_anchor = {
        q["result_name"]: parity.run_named_query(con, q["sql"], ANCHOR_DATE)
        for q in definition["queries"]
    }
    anchor_html = render.render_definition(definition, at_anchor)
    anchor_tables = parity._extract_tables(anchor_html)
    original_tables = parity._extract_tables(artifact["content"])
    assert anchor_tables == original_tables

    # Regenerate as of a different date: same structure, different data.
    as_of = "2025-05-15"
    at_other = {
        q["result_name"]: parity.run_named_query(con, q["sql"], as_of)
        for q in definition["queries"]
    }
    other_html = render.render_definition(definition, at_other)
    other_tables = parity._extract_tables(other_html)

    assert set(other_tables) == set(anchor_tables)  # structure preserved
    assert other_tables["admissions_by_division"] != anchor_tables[
        "admissions_by_division"
    ]  # data changed
