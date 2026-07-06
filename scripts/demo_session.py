"""Scripted demo session: drives the tools in-process the way the Copilot client
would, saves a report definition, and prints the resulting report_id.

This mirrors the acceptance walkthrough steps 3-7. Run it, then replay with:
    python runner/regenerate.py --report-id <printed id> --as-of 2025-05-15
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runner import render  # noqa: E402
from server import tools  # noqa: E402

CID = "demo"

ADMISSIONS = (
    "SELECT f.division, COUNT(*) AS admissions "
    "FROM admissions a JOIN facilities f ON a.facility_id = f.facility_id "
    "WHERE a.admit_date >= DATE '2025-06-01' AND a.admit_date < DATE '2025-07-01' "
    "GROUP BY f.division ORDER BY f.division"
)
CENSUS = (
    "SELECT f.facility_name, ROUND(AVG(c.midnight_census), 1) AS avg_census, "
    "ROUND(AVG(c.midnight_census * 1.0 / f.bed_count), 4) AS occupancy_rate "
    "FROM daily_census c JOIN facilities f ON c.facility_id = f.facility_id "
    "WHERE c.census_date >= DATE '2025-06-01' AND c.census_date < DATE '2025-07-01' "
    "GROUP BY f.facility_name ORDER BY f.facility_name"
)
OVERALL = (
    "SELECT ROUND(AVG(c.midnight_census * 1.0 / f.bed_count), 4) AS occupancy_rate "
    "FROM daily_census c JOIN facilities f ON c.facility_id = f.facility_id "
    "WHERE c.census_date >= DATE '2025-06-01' AND c.census_date < DATE '2025-07-01'"
)


def main() -> None:
    # Step 5: a throwaway query, never referenced.
    tools.execute_sql(CID, "SELECT COUNT(*) AS n FROM admissions", "scratch")

    # Steps 3-4: the two real queries.
    adm = tools.execute_sql(CID, ADMISSIONS, "admissions_by_division")
    cen = tools.execute_sql(CID, CENSUS, "census_by_facility")
    ovr = tools.execute_sql(CID, OVERALL, "overall_occupancy")
    print(f"admissions_by_division: {adm['row_count']} rows")
    print(f"census_by_facility:     {cen['row_count']} rows")

    # Step 6: build the artifact with data attributes.
    content = (
        "<h1>Division Admissions and Census (last 30 days)</h1>"
        "<h2>Admissions by division</h2>"
        + render.build_table_html(
            "admissions_by_division", adm["columns"], adm["rows"]
        )
        + "<h2>Census and occupancy by facility</h2>"
        + render.build_table_html("census_by_facility", cen["columns"], cen["rows"])
        + "<p class='headline'>Overall occupancy: "
        + render.build_value_span(
            "overall_occupancy", "occupancy_rate", ovr["rows"][0]["occupancy_rate"]
        )
        + "</p>"
    )
    artifact = {
        "format": "html",
        "title": "Division Admissions and Census",
        "content": content,
    }

    # Step 7: save.
    result = tools.save_report_definition(
        CID,
        "Division Admissions and Census",
        transcript=[
            {"role": "user", "content": "admissions by division, last 30 days"},
            {"role": "assistant", "content": "here is the report"},
        ],
        final_artifact=artifact,
    )
    print(json.dumps(result, indent=2, default=str))
    print(f"\nREPORT_ID={result['report_id']}")


if __name__ == "__main__":
    main()
