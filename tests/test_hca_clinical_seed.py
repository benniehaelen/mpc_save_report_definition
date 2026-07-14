"""The seeded HCA clinical tables must let the TN monthly-encounters query run.

Contract for the three objects `data/seed.py` creates from the BigQuery DDL:
the schemas/tables/view exist, the facility view derives timezones, and the
translated query returns one row per month of the 24-month window with only
TN-hospital encounters counted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.db import get_connection

_QUERY = (
    Path(__file__).resolve().parent.parent / "data" / "queries" / "tn_monthly_encounters.sql"
).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def con():
    return get_connection()


def test_the_three_objects_exist(con):
    tables = {
        (schema, name)
        for schema, name in con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables"
        ).fetchall()
    }
    assert ("clinical_core_silver", "encounter") in tables
    assert ("pub_facility_master_silver", "facility_master_sites_silver") in tables
    # The view shows up in information_schema.tables too (table_type = VIEW).
    assert ("enterprise_ontology_gold", "facility_master_site") in tables


def test_encounter_has_the_full_column_set(con):
    cols = [
        r[0]
        for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'clinical_core_silver' AND table_name = 'encounter'"
        ).fetchall()
    ]
    assert len(cols) == 65
    # admission_date_time is stored as text (STRING in the source), and the query
    # casts it -- so it must not be a TIMESTAMP column.
    admit_type = con.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema='clinical_core_silver' AND table_name='encounter' "
        "AND column_name='admission_date_time'"
    ).fetchone()[0]
    assert admit_type.upper().startswith("VARCHAR")


def test_the_view_derives_the_timezone_columns(con):
    rows = con.execute(
        "SELECT DISTINCT time_zone_code, time_zone_iana "
        "FROM enterprise_ontology_gold.facility_master_site "
        "WHERE time_zone_code IS NOT NULL ORDER BY time_zone_code"
    ).fetchall()
    mapping = dict(rows)
    assert mapping.get("CST") == "America/Chicago"
    assert mapping.get("EST") == "America/New_York"


def test_only_tn_hospitals_pass_the_filter(con):
    tn = con.execute(
        "SELECT DISTINCT state_code, site_type "
        "FROM enterprise_ontology_gold.facility_master_site "
        "WHERE UPPER(state_code)='TN' AND UPPER(site_type) LIKE 'HOSPITAL%'"
    ).fetchall()
    assert tn == [("TN", "Hospital - General")]  # decoys (FL/TX hospitals, TN clinics) excluded


def test_the_query_returns_one_row_per_window_month(con):
    rows = con.execute(_QUERY).fetchall()
    # Window is date_trunc('month', anchor - 24mo) .. anchor = 2023-06 .. 2025-06.
    months = [m for m, _ in rows]
    assert months == [
        f"{y}-{mo:02d}"
        for y in range(2023, 2026)
        for mo in range(1, 13)
        if (y, mo) >= (2023, 6) and (y, mo) <= (2025, 6)
    ]
    assert len(rows) == 25
    assert all(count > 0 for _, count in rows)


def test_the_curated_encounters_view_is_in_main_and_clean(con):
    """The plain-English demo view: discoverable in main, one row per current encounter."""
    from server import tools

    catalog = tools._schema_catalog(con)  # what nl_query surfaces (main schema only)
    assert "encounters" in catalog
    assert set(catalog["encounters"]) == {
        "encounter_date", "facility", "state", "facility_type",
        "time_zone", "coid", "encounter_id",
    }
    # A plain count(*) on the view matches the distinct-key count on the raw table.
    via_view = con.execute(
        "SELECT count(*) FROM encounters WHERE upper(state)='TN' "
        "AND upper(facility_type) LIKE 'HOSPITAL%'"
    ).fetchone()[0]
    via_raw = con.execute(
        """
        SELECT COUNT(DISTINCT e.coid || '|' || e.patient_account_num)
        FROM clinical_core_silver.encounter e
        WHERE e.latest_record_ind = 1 AND e.patient_account_num IS NOT NULL
          AND TRY_CAST(e.admission_date_time AS TIMESTAMP) IS NOT NULL
          AND e.coid IN (
            SELECT coid FROM enterprise_ontology_gold.facility_master_site
            WHERE UPPER(state_code)='TN' AND UPPER(site_type) LIKE 'HOSPITAL%'
          )
        """
    ).fetchone()[0]
    assert via_view == via_raw > 0


def test_superseded_and_decoy_rows_are_not_counted(con):
    # Every counted encounter is a latest_record_ind=1 row on a TN-hospital coid.
    total_query = con.execute(
        "SELECT SUM(total_encounters) FROM (" + _QUERY.rstrip().rstrip(";") + ")"
    ).fetchone()[0]
    counted_directly = con.execute(
        """
        SELECT COUNT(DISTINCT e.coid || '|' || e.patient_account_num)
        FROM clinical_core_silver.encounter e
        WHERE e.latest_record_ind = 1
          AND TRY_CAST(e.admission_date_time AS TIMESTAMP) IS NOT NULL
          AND e.coid IN (
            SELECT coid FROM enterprise_ontology_gold.facility_master_site
            WHERE UPPER(state_code)='TN' AND UPPER(site_type) LIKE 'HOSPITAL%'
          )
        """
    ).fetchone()[0]
    assert total_query == counted_directly
