"""Parity gate tests: matching artifacts pass, edited cells fail and are named,
markup-only differences pass.
"""

from __future__ import annotations

from server import call_log, compiler, parity, tools
from server.db import ANCHOR_DATE, get_connection, get_meta_connection
from runner import render

_ADMISSIONS = (
    "SELECT f.division, COUNT(*) AS admissions "
    "FROM admissions a JOIN facilities f ON a.facility_id = f.facility_id "
    "WHERE a.admit_date >= DATE '2025-06-01' AND a.admit_date < DATE '2025-07-01' "
    "GROUP BY f.division ORDER BY f.division"
)
_OCCUPANCY = (
    "SELECT ROUND(AVG(c.midnight_census * 1.0 / f.bed_count), 4) AS occupancy_rate "
    "FROM daily_census c JOIN facilities f ON c.facility_id = f.facility_id "
    "WHERE c.census_date >= DATE '2025-06-01' AND c.census_date < DATE '2025-07-01'"
)


def _build_session(conversation_id: str):
    adm = tools.execute_sql(conversation_id, _ADMISSIONS, "admissions_by_division")
    occ = tools.execute_sql(conversation_id, _OCCUPANCY, "overall_occupancy")
    table = render.build_table_html(
        "admissions_by_division", adm["columns"], adm["rows"]
    )
    span = render.build_value_span(
        "overall_occupancy", "occupancy_rate", occ["rows"][0]["occupancy_rate"]
    )
    content = f"<h1>Admissions</h1>{table}<p class='headline'>{span}</p>"
    artifact = {"format": "html", "title": "Admissions", "content": content}
    return artifact


def _distill(conversation_id: str, artifact: dict) -> dict:
    log_rows = call_log.fetch(get_meta_connection(), conversation_id)
    return compiler.distill(
        report_name="Admissions",
        transcript=[],
        final_artifact=artifact,
        log_rows=log_rows,
        anchor_date=ANCHOR_DATE,
    )


def test_matching_artifact_passes():
    artifact = _build_session("parity-pass")
    definition = _distill("parity-pass", artifact)
    result = parity.check(get_connection(), definition, artifact, ANCHOR_DATE)
    assert result["passed"], result["diff_summary"]


def test_edited_cell_fails_and_names_table():
    artifact = _build_session("parity-edit")
    definition = _distill("parity-edit", artifact)

    # Corrupt the first data cell in the admissions table.
    first_td_open = artifact["content"].index("<td>") + len("<td>")
    first_td_close = artifact["content"].index("</td>", first_td_open)
    tampered_content = (
        artifact["content"][:first_td_open]
        + "ZZZ_WRONG"
        + artifact["content"][first_td_close:]
    )
    tampered = {**artifact, "content": tampered_content}

    result = parity.check(get_connection(), definition, tampered, ANCHOR_DATE)
    assert not result["passed"]
    assert "admissions_by_division" in result["diff_summary"]


def test_markup_only_difference_passes():
    artifact = _build_session("parity-markup")
    definition = _distill("parity-markup", artifact)

    # Add a class attribute and extra whitespace: markup changes, values do not.
    restyled = artifact["content"].replace(
        '<table data-result="admissions_by_division">',
        '<table class="fancy"   data-result="admissions_by_division">',
    )
    restyled = {**artifact, "content": restyled}

    result = parity.check(get_connection(), definition, restyled, ANCHOR_DATE)
    assert result["passed"], result["diff_summary"]
