"""Create and populate the local databases with deterministic synthetic data.

Run: python data/seed.py

Produces data/poc.duckdb with 120 days of healthcare-flavored data ending on the
fixed anchor date of 2025-06-30, and resets data/poc_meta.sqlite -- the metadata
store holding the empty platform tables the server writes.

The `main` schema holds the report world (facilities, admissions, daily_census,
marketshare_volume, plus the knowledge-graph catalog). Three further schemas hold
the HCA clinical tables translated from the BigQuery DDL in query.sql --
`clinical_core_silver.encounter`, `pub_facility_master_silver.facility_master_sites_silver`,
and the `enterprise_ontology_gold.facility_master_site` view -- seeded so
`data/queries/tn_monthly_encounters.sql` returns monthly TN encounter counts. A
curated `main.encounters` view sits on top of those, presenting one clean row per
current encounter so non-experts can query it in plain English.

The script is idempotent: it drops and recreates everything on each run.

This is the only place that opens poc.duckdb read-write, and it takes an
exclusive lock while it does. Nothing else may be attached to the warehouse.
"""

from __future__ import annotations

import datetime as dt
import random
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.db import reset_meta_store  # noqa: E402

ANCHOR_DATE = dt.date(2025, 6, 30)
NUM_DAYS = 120
DB_PATH = Path(__file__).resolve().parent / "poc.duckdb"

DIVISIONS = ["North", "Central", "South"]
SERVICE_LINES = ["Medical", "Surgical", "Emergency", "Obstetrics"]

FACILITY_NAMES = [
    "Riverside General", "Lakeview Medical", "Summit Health", "Pinecrest Hospital",
    "Cedar Valley Care", "Harborview Center", "Fairmont Regional", "Brookside General",
    "Highland Medical", "Meadowbrook Health", "Stonegate Hospital", "Westfield Care",
]

# ---------------------------------------------------------------------------
# The market-share world (marketshare_volume)
# ---------------------------------------------------------------------------
# 20 quarters, Q2'20 .. Q1'25. The last complete quarter before the anchor
# (2025-06-30) is Q1'25, and the anchor's own quarter (Q2'25) is deliberately
# absent: report windows end *exclusive* of the quarter containing the report
# date, so the data must stop where the windows stop.
FIRST_QUARTER = dt.date(2020, 4, 1)
NUM_QUARTERS = 20

HEALTH_SYSTEMS = [
    ("HCA Healthcare", True),
    ("Universal Health Services", False),
    ("Dignity Health", False),
    ("University Medical Center", False),
    ("Prime Healthcare", False),
    ("West Henderson Hospital", False),
]
CATEGORIES = ["ER Admissions", "Surgical", "Medical"]

SURGICAL_ESLS = [
    "GENERAL SURGERY", "ORTHOPEDICS", "SPINE (SURGICAL)", "OBSTETRICS", "UROLOGY",
    "GYNECOLOGY", "NEUROSURGERY", "VASCULAR (SURGICAL)", "CARDIAC (SURGICAL)",
    "THORACIC SURGERY", "ONCOLOGY (SURGICAL)",
]
MEDICAL_ESLS = [
    "CARDIAC (PROCEDURAL)", "GASTROENTEROLOGY", "ONCOLOGY (MEDICAL)",
    "CARDIAC (MEDICAL)", "VASCULAR (PROCEDURAL)", "NEUROLOGY",
]
ESL_LEVEL_2 = SURGICAL_ESLS + MEDICAL_ESLS

# Market size per category: (base at Q2'20, value at Q1'25). The surgical market
# contracts while the medical market grows -- that growth is what a new entrant
# (West Henderson) shows up in.
_MARKET_CATEGORY = {
    "ER Admissions": (44000, 47000),
    "Surgical": (7300, 6700),
    "Medical": (8600, 9600),
}
# HCA's share of each category: ER flat, surgical rising, medical compressing.
_HCA_CATEGORY_SHARE = {
    "ER Admissions": (0.345, 0.345),
    "Surgical": (0.230, 0.270),
    "Medical": (0.490, 0.450),
}

# Relative market weight of each ESL, and how fast that ESL's market grows.
# Orthopedics grows hardest: HCA loses share in a *growing* market, which is the
# uncomfortable half of the story.
_ESL_MARKET_WEIGHT = {
    "GENERAL SURGERY": 16.0, "ORTHOPEDICS": 7.7, "SPINE (SURGICAL)": 3.3,
    "OBSTETRICS": 8.3, "UROLOGY": 2.4, "GYNECOLOGY": 1.1, "NEUROSURGERY": 1.7,
    "VASCULAR (SURGICAL)": 1.5, "CARDIAC (SURGICAL)": 1.8, "THORACIC SURGERY": 0.9,
    "ONCOLOGY (SURGICAL)": 1.0, "CARDIAC (PROCEDURAL)": 2.0,
    "GASTROENTEROLOGY": 2.8, "ONCOLOGY (MEDICAL)": 1.6, "CARDIAC (MEDICAL)": 1.9,
    "VASCULAR (PROCEDURAL)": 1.4, "NEUROLOGY": 1.6,
}
_ESL_MARKET_GROWTH = {esl: 0.08 for esl in ESL_LEVEL_2}
_ESL_MARKET_GROWTH["ORTHOPEDICS"] = 1.20

# HCA's relative pull within a category, before the trend kicks in.
_ESL_HCA_WEIGHT = {
    "GENERAL SURGERY": 33.0, "ORTHOPEDICS": 35.1, "SPINE (SURGICAL)": 45.4,
    "OBSTETRICS": 17.5, "UROLOGY": 43.0, "GYNECOLOGY": 47.0, "NEUROSURGERY": 47.6,
    "VASCULAR (SURGICAL)": 40.8, "CARDIAC (SURGICAL)": 31.6, "THORACIC SURGERY": 48.0,
    "ONCOLOGY (SURGICAL)": 38.4, "CARDIAC (PROCEDURAL)": 51.7,
    "GASTROENTEROLOGY": 26.6, "ONCOLOGY (MEDICAL)": 38.0, "CARDIAC (MEDICAL)": 26.0,
    "VASCULAR (PROCEDURAL)": 39.8, "NEUROLOGY": 31.3,
}
# Exactly five ESLs lose share. Orthopedics is driven by its own curve below; the
# other four carry a negative trend. Everything else gains -- 12 of 17.
# A category's HCA cases are fixed by its share curve, so the trends only decide
# how that fixed pool is redistributed: gains and losses inside a category are
# zero-sum, which is why each category needs at least one loser.
_ESL_SHARE_LOSERS = {
    "OBSTETRICS": -0.16,
    "CARDIAC (PROCEDURAL)": -0.16,
    "VASCULAR (PROCEDURAL)": -0.16,
    "NEUROLOGY": -0.16,
}
_ESL_SHARE_GAIN_TREND = 0.30
_ORTHO_SHARE = (0.355, 0.300)  # over the last 8 quarters only

# The leader gap, in cases per quarter: UHS is #1 throughout, but only just.
_GAP_START, _GAP_END = 4000, 700
# West Henderson opens in Q3'24 and ramps fast, medical only.
_WEST_HENDERSON_RAMP = {17: 200, 18: 650, 19: 1400}
_OTHER_SYSTEM_WEIGHT = {
    "Dignity Health": 0.45,
    "University Medical Center": 0.32,
    "Prime Healthcare": 0.23,
}

PATIENT_TYPE_CODE = "I"
POPULATION_TYPE_NAME = "Service Area"
MARKET_CODE = "00024"


def _drop_and_create(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("DROP TABLE IF EXISTS facilities")
    con.execute("DROP TABLE IF EXISTS admissions")
    con.execute("DROP TABLE IF EXISTS daily_census")
    con.execute("DROP TABLE IF EXISTS marketshare_volume")

    con.execute(
        """
        CREATE TABLE facilities (
          facility_id   INTEGER PRIMARY KEY,
          facility_name VARCHAR,
          division      VARCHAR,
          bed_count     INTEGER
        )
        """
    )
    con.execute(
        """
        CREATE TABLE admissions (
          admission_id   INTEGER PRIMARY KEY,
          facility_id    INTEGER,
          admit_date     DATE,
          service_line   VARCHAR,
          length_of_stay INTEGER
        )
        """
    )
    con.execute(
        """
        CREATE TABLE daily_census (
          facility_id     INTEGER,
          census_date     DATE,
          midnight_census INTEGER
        )
        """
    )
    con.execute(
        """
        CREATE TABLE marketshare_volume (
          period_quarter       DATE,
          health_system        VARCHAR,
          is_hca               BOOLEAN,
          category             VARCHAR,
          esl_level_2          VARCHAR,
          patient_type_code    VARCHAR,
          population_type_name VARCHAR,
          market_code          VARCHAR,
          cases                INTEGER
        )
        """
    )
    # tool_call_log and report_definitions deliberately do NOT live here. They
    # are the only tables the server writes, and keeping them in the DuckDB file
    # would force the server to hold an exclusive read-write lock on the whole
    # warehouse. They live in the SQLite metadata store instead, reset below.

    # The local "knowledge graph": governed metrics and their ValueSets, plus
    # the mapping from a dimension column to the ValueSet that governs it.
    con.execute(
        """
        CREATE TABLE metrics (
          metric_id    VARCHAR PRIMARY KEY,
          display_name VARCHAR,
          unit         VARCHAR,
          description  VARCHAR
        )
        """
    )
    con.execute(
        "CREATE TABLE value_sets (value_set VARCHAR, code VARCHAR)"
    )
    con.execute(
        "CREATE TABLE dimension_value_sets (dimension VARCHAR, value_set VARCHAR)"
    )


def _catalog_rows():
    metrics = [
        ("admissions", "Admissions", "count", "Count of admissions"),
        ("avg_los", "Average length of stay", "days", "Mean length of stay"),
        ("avg_census", "Average midnight census", "count", "Mean midnight census"),
        ("occupancy_rate", "Occupancy rate", "ratio", "Census over bed count"),
        ("cases", "Cases", "count", "Volume of cases"),
        ("market_cases", "Market cases", "count", "Total market cases in scope"),
        ("hca_cases", "HCA cases", "count", "HCA cases in scope"),
        ("share_pct", "Market share", "ratio", "HCA cases over market cases"),
        ("gap", "Leader gap", "count", "Leader minus HCA quarterly cases"),
        ("gap_trend", "Gap trend", "count", "Change in the leader gap vs the prior quarter"),
        ("share_change_pp", "Share change", "ratio", "Change in share, in percentage points"),
        ("vol_change", "Volume change", "count", "Change in cases vs the prior period"),
    ]
    value_sets = (
        [("divisions", d) for d in DIVISIONS]
        + [("service_lines", s) for s in SERVICE_LINES]
        + [("health_systems", s) for s, _is_hca in HEALTH_SYSTEMS]
        + [("categories", c) for c in CATEGORIES]
        + [("esl_level_2", e) for e in ESL_LEVEL_2]
    )
    dimension_value_sets = [
        ("division", "divisions"),
        ("service_line", "service_lines"),
        ("health_system", "health_systems"),
        ("category", "categories"),
        ("esl_level_2", "esl_level_2"),
        # Alias: result sets often shorten the column to `esl`.
        ("esl", "esl_level_2"),
    ]
    return metrics, value_sets, dimension_value_sets


def quarter_date(index: int) -> dt.date:
    """First day of the `index`-th quarter after FIRST_QUARTER."""
    months = (FIRST_QUARTER.year * 12 + FIRST_QUARTER.month - 1) + 3 * index
    return dt.date(months // 12, months % 12 + 1, 1)


def _lerp(start: float, end: float, t: float) -> float:
    return start + (end - start) * t


def _apportion(total: int, weights: list[float]) -> list[int]:
    """Split `total` across `weights` as integers that sum to exactly `total`.

    Largest-remainder, so the seeded story survives rounding: a category's HCA
    cases are the sum of its ESLs, and the parity gate compares those sums.
    """
    if total <= 0 or not weights or sum(weights) <= 0:
        return [0] * len(weights)
    scale = total / sum(weights)
    exact = [w * scale for w in weights]
    shares = [int(value) for value in exact]
    for index in sorted(
        range(len(exact)), key=lambda i: exact[i] - shares[i], reverse=True
    )[: total - sum(shares)]:
        shares[index] += 1
    return shares


def _gap_target(index: int) -> int:
    """Cases by which UHS leads HCA in quarter `index`. Narrows, never closes."""
    return round(_lerp(_GAP_START, _GAP_END, index / (NUM_QUARTERS - 1)))


def _build_marketshare_rows(rng: random.Random) -> list[tuple]:
    """Generate marketshare_volume top-down so the story is exact, not emergent.

    Category totals and HCA's share of them are set directly, so the ER/surgical/
    medical share curves hold by construction. ESL volumes are then apportioned
    *inside* each category, so per-ESL gains and losses redistribute a fixed pool.
    Finally UHS is pinned at ``HCA + gap``, which makes the leader gap exact
    rather than the accidental sum of six systems' rounding.
    """
    rows: list[tuple] = []

    for index in range(NUM_QUARTERS):
        t = index / (NUM_QUARTERS - 1)
        # The ESL trends only bite over the last eight quarters -- the window the
        # report's year-over-year comparison actually looks at.
        phase = min(1.0, max(0.0, (index - 11) / 8.0))
        period = quarter_date(index)

        def noise() -> float:
            return 1.0 + rng.uniform(-0.03, 0.03)

        def share_wobble() -> float:
            return rng.uniform(-0.0015, 0.0015)

        market_cat = {
            cat: round(_lerp(lo, hi, t) * noise())
            for cat, (lo, hi) in _MARKET_CATEGORY.items()
        }
        hca_cat = {
            cat: round(market_cat[cat] * (_lerp(lo, hi, t) + share_wobble()))
            for cat, (lo, hi) in _HCA_CATEGORY_SHARE.items()
        }

        market: dict[str, int] = {}
        for category, esls in (("Surgical", SURGICAL_ESLS), ("Medical", MEDICAL_ESLS)):
            weights = [
                _ESL_MARKET_WEIGHT[e] * (1 + _ESL_MARKET_GROWTH[e] * t) * noise()
                for e in esls
            ]
            market.update(zip(esls, _apportion(market_cat[category], weights)))

        hca: dict[str, int] = {}
        # Orthopedics rides its own share curve, so its decline is exact.
        hca["ORTHOPEDICS"] = round(market["ORTHOPEDICS"] * _lerp(*_ORTHO_SHARE, phase))
        for category, esls in (("Surgical", SURGICAL_ESLS), ("Medical", MEDICAL_ESLS)):
            rest = [e for e in esls if e != "ORTHOPEDICS"]
            pool = hca_cat[category] - (
                hca["ORTHOPEDICS"] if category == "Surgical" else 0
            )
            weights = [
                market[e]
                * _ESL_HCA_WEIGHT[e]
                / 100.0
                * (1 + _ESL_SHARE_LOSERS.get(e, _ESL_SHARE_GAIN_TREND) * phase)
                for e in rest
            ]
            for esl, cases in zip(rest, _apportion(pool, weights)):
                # Never let HCA take a whole ESL; there must be a market to share.
                hca[esl] = min(cases, int(market[esl] * 0.92))

        hca_er = hca_cat["ER Admissions"]
        market_er = market_cat["ER Admissions"]
        hca_total = hca_er + sum(hca.values())
        market_total = market_er + sum(market.values())
        non_hca_total = market_total - hca_total

        uhs_total = hca_total + _gap_target(index) + rng.randint(-40, 40)
        if not 0 < uhs_total < non_hca_total:
            raise AssertionError(
                f"quarter {index}: UHS total {uhs_total} does not fit the "
                f"non-HCA market of {non_hca_total}"
            )

        def emit(system: str, is_hca: bool, category: str, esl: str | None, cases: int):
            if cases > 0:
                rows.append(
                    (
                        period, system, is_hca, category, esl,
                        PATIENT_TYPE_CODE, POPULATION_TYPE_NAME, MARKET_CODE, cases,
                    )
                )

        emit("HCA Healthcare", True, "ER Admissions", None, hca_er)
        for esl in SURGICAL_ESLS:
            emit("HCA Healthcare", True, "Surgical", esl, hca[esl])
        for esl in MEDICAL_ESLS:
            emit("HCA Healthcare", True, "Medical", esl, hca[esl])

        cells = (
            [("ER Admissions", None, market_er - hca_er)]
            + [("Surgical", e, market[e] - hca[e]) for e in SURGICAL_ESLS]
            + [("Medical", e, market[e] - hca[e]) for e in MEDICAL_ESLS]
        )
        remainders = {}
        uhs_alloc = _apportion(uhs_total, [max(rem, 0) for _c, _e, rem in cells])
        for (category, esl, remainder), uhs_cases in zip(cells, uhs_alloc):
            emit("Universal Health Services", False, category, esl, uhs_cases)
            remainders[(category, esl)] = remainder - uhs_cases

        # West Henderson opens medical-only, spread across every medical ESL.
        # Carving it out of a single ESL's leftovers would silently truncate the
        # ramp to whatever that one line happened to have spare.
        target = _WEST_HENDERSON_RAMP.get(index, 0)
        if target:
            keys = [("Medical", esl) for esl in MEDICAL_ESLS]
            available = [remainders[key] for key in keys]
            if sum(available) < target * 1.2:
                raise AssertionError(
                    f"quarter {index}: West Henderson needs {target} cases but the "
                    f"medical remainder is only {sum(available)}"
                )
            allocated = _apportion(target, available)
            if sum(allocated) != target:
                raise AssertionError("West Henderson ramp lost cases to rounding")
            for key, cases in zip(keys, allocated):
                emit("West Henderson Hospital", False, key[0], key[1], cases)
                remainders[key] -= cases

        for (category, esl), pool in remainders.items():
            weights = list(_OTHER_SYSTEM_WEIGHT.values())
            for system, cases in zip(_OTHER_SYSTEM_WEIGHT, _apportion(pool, weights)):
                emit(system, False, category, esl, cases)

    return rows


def _build_rows(rng: random.Random):
    facilities = []
    for i in range(12):
        facility_id = i + 1
        division = DIVISIONS[i % 3]
        bed_count = rng.randint(80, 400)
        facilities.append((facility_id, FACILITY_NAMES[i], division, bed_count))

    admissions = []
    census = []
    admission_id = 0
    dates = [ANCHOR_DATE - dt.timedelta(days=NUM_DAYS - 1 - d) for d in range(NUM_DAYS)]

    for facility_id, _name, _division, bed_count in facilities:
        scale = bed_count / 240.0
        for day in dates:
            daily = max(1, int(rng.randint(40, 120) * scale))
            for _ in range(daily):
                admission_id += 1
                los = min(14, 1 + int(rng.expovariate(1 / 3.0)))
                admissions.append(
                    (admission_id, facility_id, day, rng.choice(SERVICE_LINES), los)
                )
            occ = rng.uniform(0.55, 0.90)
            census.append((facility_id, day, min(bed_count, int(bed_count * occ))))

    return facilities, admissions, census


# ---------------------------------------------------------------------------
# The HCA clinical world (translated from the BigQuery DDL in
# c:\Users\benni\Downloads\query.sql). Three objects that the "monthly TN
# encounters" query joins:
#   clinical_core_silver.encounter                     (fact-ish clinical table)
#   pub_facility_master_silver.facility_master_sites_silver  (facility master)
#   enterprise_ontology_gold.facility_master_site      (view, adds timezone cols)
#
# BigQuery -> DuckDB: three-part project.dataset.table names lose the project
# prefix (DuckDB has one catalog per file); each dataset becomes a schema.
# STRING->VARCHAR, INT64->BIGINT, NUMERIC->DECIMAL(38,9), DATETIME/TIMESTAMP->
# TIMESTAMP. OPTIONS/CLUSTER BY/PARTITION BY are dropped. `admission_date_time`
# stays VARCHAR (STRING in the source; the query SAFE_CASTs it).
# ---------------------------------------------------------------------------

# TN hospitals that pass the query filter (state_code='TN', site_type LIKE
# 'HOSPITAL%'). Their coids are what the encounter counts are grouped from.
_TN_HOSPITALS = [
    "Nashville General", "Memphis Methodist", "Knoxville Regional",
    "Chattanooga Erlanger", "Franklin Williamson", "Murfreesboro Saint Thomas",
    "Clarksville Tennova", "Jackson Madison", "Johnson City Medical",
    "Kingsport Holston", "Cookeville Regional", "Hendersonville Sumner",
    "Brentwood TriStar", "Smyrna Stonecrest", "Columbia Maury",
]
# Decoys, so the query's WHERE actually filters: non-TN hospitals, and TN sites
# that are not hospitals. None of these should appear in the counts.
_DECOY_SITES = [
    ("Orlando Health", "FL", "Hospital - General"),
    ("Dallas Presbyterian", "TX", "Hospital - General"),
    ("Denver Mercy", "CO", "Hospital - General"),
    ("Nashville Family Clinic", "TN", "Clinic"),
    ("Memphis Surgery Center", "TN", "Ambulatory Surgery Center"),
    ("Knoxville Imaging", "TN", "Imaging Center"),
]

_HCA_WINDOW_START = (2023, 6)   # DATE_TRUNC(anchor - 24 months, MONTH) = 2023-06-01
_HCA_WINDOW_END = (2025, 6)     # up to the anchor month; 25 months inclusive
_ENCOUNTERS_PER_SITE_MONTH = 30


def _create_hca_clinical(con: duckdb.DuckDBPyConnection) -> None:
    """Create the three HCA clinical objects, faithful to the BigQuery DDL."""
    con.execute("DROP VIEW IF EXISTS encounters")
    con.execute("DROP VIEW IF EXISTS enterprise_ontology_gold.facility_master_site")
    con.execute("DROP TABLE IF EXISTS pub_facility_master_silver.facility_master_sites_silver")
    con.execute("DROP TABLE IF EXISTS clinical_core_silver.encounter")
    for schema in (
        "clinical_core_silver",
        "pub_facility_master_silver",
        "enterprise_ontology_gold",
    ):
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

    con.execute(
        """
        CREATE TABLE pub_facility_master_silver.facility_master_sites_silver (
          company_code                 VARCHAR NOT NULL,
          coid                         VARCHAR NOT NULL,
          parent_site_code             VARCHAR NOT NULL,
          site_code                    VARCHAR NOT NULL,
          site_type                    VARCHAR,
          site_operating_status        VARCHAR,
          site_name                    VARCHAR,
          site_short_name              VARCHAR,
          street_address_1             VARCHAR,
          street_address_2             VARCHAR,
          city                         VARCHAR,
          county                       VARCHAR,
          state_code                   VARCHAR,
          postal_code                  VARCHAR,
          country_code                 VARCHAR,
          latitude                     DECIMAL(38, 9),
          longitude                    DECIMAL(38, 9),
          site_timezone                VARCHAR,
          effective_date_time          TIMESTAMP NOT NULL,
          source_system_ref_code       VARCHAR NOT NULL,
          bronze_ingest_date_time      TIMESTAMP NOT NULL,
          silver_ingest_date_time      TIMESTAMP NOT NULL,
          silver_last_update_date_time TIMESTAMP NOT NULL
        )
        """
    )

    con.execute(
        """
        CREATE TABLE clinical_core_silver.encounter (
          company_code                       VARCHAR NOT NULL,
          network_mnemonic                   VARCHAR NOT NULL,
          facility_mnemonic                  VARCHAR NOT NULL,
          coid                               VARCHAR NOT NULL,
          patient_account_num                VARCHAR NOT NULL,
          pa_patient_account_num             VARCHAR,
          medical_record_num                 VARCHAR,
          medical_record_urn                 VARCHAR,
          admission_date_time                VARCHAR,
          admission_date_time_utc            VARCHAR,
          discharge_date_time                VARCHAR,
          discharge_date_time_utc            VARCHAR,
          reason_for_visit                   VARCHAR,
          patient_class_code                 VARCHAR,
          patient_class_desc                 VARCHAR,
          patient_type_code                  VARCHAR,
          patient_type_desc                  VARCHAR,
          admit_source_code                  VARCHAR,
          admit_source_desc                  VARCHAR,
          mode_of_arrival_code               VARCHAR,
          mode_of_arrival_code_desc          VARCHAR,
          admission_type_code                VARCHAR,
          admission_type_desc                VARCHAR,
          discharge_disposition_code         VARCHAR,
          confidentiality_code               VARCHAR,
          confidentiality_desc               VARCHAR,
          hospital_service_code              VARCHAR,
          hospital_service_desc              VARCHAR,
          inpatient_length_of_stay_day_count VARCHAR,
          patient_birth_date                 VARCHAR,
          deceased_date_time                 VARCHAR,
          deceased_date_time_utc             VARCHAR,
          patient_deceased_ind               VARCHAR,
          patient_status_code                VARCHAR,
          emr_patient_id                     VARCHAR,
          emr_patient_id_domain              VARCHAR,
          financial_class_code               VARCHAR,
          financial_class_desc               VARCHAR,
          accommodation_code                 VARCHAR,
          alternate_encounter_id             VARCHAR,
          mother_patient_account_num         VARCHAR,
          message_type_event_code            VARCHAR,
          source_event_code                  VARCHAR,
          modified_event_code                VARCHAR,
          event_occurred_date_time           TIMESTAMP,
          event_occurred_date_time_utc       TIMESTAMP,
          event_recorded_date_time           TIMESTAMP,
          event_recorded_date_time_utc       TIMESTAMP,
          location_unit_code                 VARCHAR,
          location_room_code                 VARCHAR,
          location_bed_code                  VARCHAR,
          event_user_id                      VARCHAR,
          prior_location_unit_code           VARCHAR,
          prior_location_room_code           VARCHAR,
          prior_location_bed_code            VARCHAR,
          prior_patient_account_num          VARCHAR,
          prior_medical_record_num           VARCHAR,
          prior_medical_record_urn           VARCHAR,
          latest_record_ind                  BIGINT NOT NULL,
          message_control_id                 VARCHAR NOT NULL,
          message_date_time                  TIMESTAMP NOT NULL,
          source_system_ref_code             VARCHAR NOT NULL,
          bronze_ingest_date_time            TIMESTAMP NOT NULL,
          silver_ingest_date_time            TIMESTAMP NOT NULL,
          silver_last_update_date_time       TIMESTAMP NOT NULL
        )
        """
    )

    # The gold view: passes the silver columns through and derives the IANA zone
    # and UTC offsets from site_timezone. Verbatim translation of the BQ CASE.
    con.execute(
        """
        CREATE VIEW enterprise_ontology_gold.facility_master_site AS
        SELECT
          company_code, coid, parent_site_code, site_code, site_type,
          site_operating_status, site_name, site_short_name, street_address_1,
          street_address_2, city, county, state_code, postal_code, country_code,
          latitude, longitude,
          site_timezone AS time_zone_code,
          CAST(CASE TRIM(UPPER(site_timezone))
            WHEN 'GMT'  THEN 'Europe/London'
            WHEN 'EST'  THEN 'America/New_York'
            WHEN 'CST'  THEN 'America/Chicago'
            WHEN 'PST'  THEN 'America/Los_Angeles'
            WHEN 'MST'  THEN 'America/Denver'
            WHEN 'AKST' THEN 'America/Anchorage'
            ELSE NULL END AS VARCHAR) AS time_zone_iana,
          CAST(CASE TRIM(UPPER(site_timezone))
            WHEN 'GMT'  THEN '+00:00' WHEN 'EST' THEN '-05:00'
            WHEN 'CST'  THEN '-06:00' WHEN 'MST' THEN '-07:00'
            WHEN 'PST'  THEN '-08:00' WHEN 'AKST' THEN '-09:00'
            ELSE NULL END AS VARCHAR) AS time_zone_standard_time_utc_offset,
          CAST(CASE TRIM(UPPER(site_timezone))
            WHEN 'GMT'  THEN '+00:00' WHEN 'EST' THEN '-04:00'
            WHEN 'CST'  THEN '-05:00' WHEN 'MST' THEN '-06:00'
            WHEN 'PST'  THEN '-07:00' WHEN 'AKST' THEN '-08:00'
            ELSE NULL END AS VARCHAR) AS time_zone_daylight_time_utc_offset,
          effective_date_time, source_system_ref_code
        FROM pub_facility_master_silver.facility_master_sites_silver
        """
    )

    # A curated analytics view in `main`, so non-experts can query encounters in
    # plain English without knowing the clinical schemas. It hides everything a
    # beginner should not have to know: the cross-schema join to the facility
    # master, the text->timestamp cast on admission_date_time, the
    # latest_record_ind current-version filter, and the non-null account guard.
    # One clean row per current encounter, with a real DATE and the facility's
    # name/state/type/timezone attached. Because it lives in `main`, nl_query
    # surfaces it like any other table.
    con.execute(
        """
        CREATE VIEW encounters AS
        SELECT
          CAST(TRY_CAST(e.admission_date_time AS TIMESTAMP) AS DATE) AS encounter_date,
          f.site_name      AS facility,
          f.state_code     AS state,
          f.site_type      AS facility_type,
          f.time_zone_iana AS time_zone,
          e.coid,
          e.patient_account_num AS encounter_id
        FROM clinical_core_silver.encounter e
        JOIN enterprise_ontology_gold.facility_master_site f ON e.coid = f.coid
        WHERE e.latest_record_ind = 1
          AND e.patient_account_num IS NOT NULL
          AND TRY_CAST(e.admission_date_time AS TIMESTAMP) IS NOT NULL
        """
    )


def _iter_months(start: tuple[int, int], end: tuple[int, int]):
    year, month = start
    while (year, month) <= end:
        yield year, month
        month += 1
        if month == 13:
            month, year = 1, year + 1


# The columns we populate. The tables carry the full DDL schema; the rest of the
# columns are left NULL. All NOT NULL columns are included here.
_FACILITY_COLUMNS = [
    "company_code", "coid", "parent_site_code", "site_code", "site_type",
    "site_operating_status", "site_name", "city", "state_code", "postal_code",
    "country_code", "site_timezone", "effective_date_time", "source_system_ref_code",
    "bronze_ingest_date_time", "silver_ingest_date_time", "silver_last_update_date_time",
]
_ENCOUNTER_COLUMNS = [
    "company_code", "network_mnemonic", "facility_mnemonic", "coid",
    "patient_account_num", "admission_date_time", "patient_class_code",
    "patient_class_desc", "admission_type_code", "latest_record_ind",
    "message_control_id", "message_date_time", "source_system_ref_code",
    "bronze_ingest_date_time", "silver_ingest_date_time", "silver_last_update_date_time",
]


def _build_hca_rows(rng: random.Random):
    """Facilities and encounters for the TN monthly-encounters query.

    Returns (facility_rows, encounter_rows) as tuples in _FACILITY_COLUMNS /
    _ENCOUNTER_COLUMNS order. TN hospitals get real monthly encounter volume;
    decoy sites, superseded (latest_record_ind=0) rows, and a few unparseable
    admission timestamps exist only so the query's filters have something to do.
    """
    ingest = dt.datetime(2025, 6, 30, 3, 0, 0)
    effective = dt.datetime(2020, 1, 1, 0, 0, 0)

    facilities = []
    tn_coids = []
    for i, name in enumerate(_TN_HOSPITALS):
        coid = f"130{i + 1:02d}"
        tn_coids.append((coid, name))
        facilities.append({
            "company_code": "H01", "coid": coid, "parent_site_code": f"S13{i + 1:05d}",
            "site_code": f"S13{i + 1:05d}", "site_type": "Hospital - General",
            "site_operating_status": "Open", "site_name": name,
            "city": name.split()[0], "state_code": "TN", "postal_code": f"37{i:03d}",
            "country_code": "US", "site_timezone": "CST" if i % 3 else "EST",
            "effective_date_time": effective, "source_system_ref_code": "RDM",
            "bronze_ingest_date_time": ingest, "silver_ingest_date_time": ingest,
            "silver_last_update_date_time": ingest,
        })
    decoy_coids = []
    for j, (name, state, site_type) in enumerate(_DECOY_SITES):
        coid = f"140{j + 1:02d}"
        decoy_coids.append(coid)
        facilities.append({
            "company_code": "H01", "coid": coid, "parent_site_code": f"S14{j + 1:05d}",
            "site_code": f"S14{j + 1:05d}", "site_type": site_type,
            "site_operating_status": "Open", "site_name": name,
            "city": name.split()[0], "state_code": state, "postal_code": f"9{j:04d}",
            "country_code": "US", "site_timezone": "CST",
            "effective_date_time": effective, "source_system_ref_code": "RDM",
            "bronze_ingest_date_time": ingest, "silver_ingest_date_time": ingest,
            "silver_last_update_date_time": ingest,
        })

    encounters = []
    account = 0

    def _encounter(coid, admit_str, latest):
        nonlocal account
        account += 1
        return {
            "company_code": "HCA", "network_mnemonic": "TN",
            "facility_mnemonic": f"F{coid}", "coid": coid,
            "patient_account_num": f"PA{account:09d}", "admission_date_time": admit_str,
            "patient_class_code": rng.choice(["I", "O", "E"]),
            "patient_class_desc": "Inpatient",
            "admission_type_code": rng.choice(["1", "2", "3"]),
            "latest_record_ind": latest, "message_control_id": f"MSG{account:09d}",
            "message_date_time": ingest, "source_system_ref_code": "MEDITECH 5.6",
            "bronze_ingest_date_time": ingest, "silver_ingest_date_time": ingest,
            "silver_last_update_date_time": ingest,
        }

    for year, month in _iter_months(_HCA_WINDOW_START, _HCA_WINDOW_END):
        days_in_month = (dt.date(year + (month // 12), (month % 12) + 1, 1)
                         - dt.timedelta(days=1)).day
        for coid, _name in tn_coids:
            count = _ENCOUNTERS_PER_SITE_MONTH + rng.randint(-8, 8)
            for _ in range(count):
                day = rng.randint(1, days_in_month)
                admit = f"{year:04d}-{month:02d}-{day:02d}T{rng.randint(0,23):02d}:{rng.randint(0,59):02d}:00"
                encounters.append(_encounter(coid, admit, 1))
            # A few superseded rows -- filtered out by latest_record_ind = 1.
            for _ in range(rng.randint(1, 3)):
                admit = f"{year:04d}-{month:02d}-15T09:00:00"
                encounters.append(_encounter(coid, admit, 0))
        # Decoy-coid encounters -- filtered out by the TN-hospital coid list.
        for _ in range(len(decoy_coids)):
            coid = rng.choice(decoy_coids)
            admit = f"{year:04d}-{month:02d}-10T12:00:00"
            encounters.append(_encounter(coid, admit, 1))
        # A couple of unparseable admission timestamps -- SAFE_CAST -> NULL,
        # filtered out by the IS NOT NULL guard.
        for _ in range(2):
            coid = rng.choice([c for c, _ in tn_coids])
            encounters.append(_encounter(coid, "UNKNOWN", 1))

    fac_rows = [tuple(f.get(c) for c in _FACILITY_COLUMNS) for f in facilities]
    enc_rows = [tuple(e.get(c) for c in _ENCOUNTER_COLUMNS) for e in encounters]
    return fac_rows, enc_rows


def main() -> None:
    rng = random.Random()
    rng.seed(42)

    if DB_PATH.exists():
        DB_PATH.unlink()

    # Clear the lineage log and definition registry so a reseed leaves no
    # definitions pointing at rows that no longer exist.
    reset_meta_store()

    con = duckdb.connect(str(DB_PATH))
    try:
        _drop_and_create(con)
        facilities, admissions, census = _build_rows(rng)
        con.executemany("INSERT INTO facilities VALUES (?, ?, ?, ?)", facilities)
        con.executemany("INSERT INTO admissions VALUES (?, ?, ?, ?, ?)", admissions)
        con.executemany("INSERT INTO daily_census VALUES (?, ?, ?)", census)

        # A separate generator: reusing `rng` here would consume its stream and
        # shift every admission and census value seeded above.
        marketshare = _build_marketshare_rows(random.Random(42))
        con.executemany(
            "INSERT INTO marketshare_volume VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            marketshare,
        )

        metrics, value_sets, dimension_value_sets = _catalog_rows()
        con.executemany("INSERT INTO metrics VALUES (?, ?, ?, ?)", metrics)
        con.executemany("INSERT INTO value_sets VALUES (?, ?)", value_sets)
        con.executemany(
            "INSERT INTO dimension_value_sets VALUES (?, ?)", dimension_value_sets
        )

        # The HCA clinical tables (own random stream, so it never shifts the data
        # seeded above).
        _create_hca_clinical(con)
        hca_facilities, hca_encounters = _build_hca_rows(random.Random(42))
        con.executemany(
            "INSERT INTO pub_facility_master_silver.facility_master_sites_silver "
            f"({', '.join(_FACILITY_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(_FACILITY_COLUMNS))})",
            hca_facilities,
        )
        con.executemany(
            "INSERT INTO clinical_core_silver.encounter "
            f"({', '.join(_ENCOUNTER_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(_ENCOUNTER_COLUMNS))})",
            hca_encounters,
        )

        print(f"Seeded {DB_PATH}")
        print(f"  facilities:   {len(facilities)}")
        print(f"  admissions:   {len(admissions)}")
        print(f"  daily_census: {len(census)}")
        print(f"  marketshare:  {len(marketshare)} rows, {NUM_QUARTERS} quarters "
              f"({quarter_date(0)} .. {quarter_date(NUM_QUARTERS - 1)})")
        print(f"  metrics:      {len(metrics)}")
        print(f"  value_sets:   {len(value_sets)} memberships")
        print(f"  hca facilities: {len(hca_facilities)} "
              f"({len(_TN_HOSPITALS)} TN hospitals + {len(_DECOY_SITES)} decoys)")
        print(f"  hca encounters: {len(hca_encounters)} rows across "
              f"{_HCA_WINDOW_START[0]}-{_HCA_WINDOW_START[1]:02d} .. "
              f"{_HCA_WINDOW_END[0]}-{_HCA_WINDOW_END[1]:02d}")
        print("  encounters view: main.encounters (curated, plain-English-friendly)")
        print(f"  anchor date:  {ANCHOR_DATE.isoformat()}")
        print("  metadata:     reset data/poc_meta.sqlite (log + registry)")
        _print_marketshare_story(con)
    finally:
        con.close()


def _print_marketshare_story(con: duckdb.DuckDBPyConnection) -> None:
    """Show the curves the report depends on, so miscalibration is visible here."""
    rows = con.execute(
        """
        WITH totals AS (
          SELECT period_quarter, health_system, SUM(cases) AS cases
          FROM marketshare_volume GROUP BY 1, 2
        ),
        cat AS (
          SELECT period_quarter, category,
                 SUM(cases) FILTER (WHERE is_hca) * 100.0 / SUM(cases) AS share
          FROM marketshare_volume GROUP BY 1, 2
        )
        SELECT t.period_quarter,
               MAX(t.cases) FILTER (WHERE t.health_system = 'HCA Healthcare') AS hca,
               MAX(t.cases) FILTER (WHERE t.health_system = 'Universal Health Services') AS uhs,
               MAX(c.share) FILTER (WHERE c.category = 'ER Admissions') AS er,
               MAX(c.share) FILTER (WHERE c.category = 'Surgical') AS surgical,
               MAX(c.share) FILTER (WHERE c.category = 'Medical') AS medical,
               COALESCE(MAX(t.cases) FILTER (WHERE t.health_system = 'West Henderson Hospital'), 0) AS west_henderson
        FROM totals t JOIN cat c USING (period_quarter)
        GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    print("\n  quarter      HCA     UHS    gap    ER%  surg%   med%  WestHenderson")
    for period, hca, uhs, er, surgical, medical, west in rows:
        print(
            f"  {period}  {hca:6d}  {uhs:6d}  {uhs - hca:5d}   "
            f"{er:4.1f}  {surgical:5.1f}  {medical:5.1f}  {west:6d}"
        )
    # Trailing four complete quarters vs the four before them -- the same window
    # the report's year-over-year ESL comparison uses.
    esl = con.execute(
        """
        WITH windowed AS (
          SELECT esl_level_2 AS esl,
                 period_quarter >= DATE '2024-04-01' AS is_current,
                 SUM(cases) FILTER (WHERE is_hca) * 100.0 / SUM(cases) AS share,
                 SUM(cases) AS market
          FROM marketshare_volume
          WHERE esl_level_2 IS NOT NULL AND period_quarter >= DATE '2023-04-01'
          GROUP BY 1, 2
        )
        SELECT esl,
               MAX(share) FILTER (WHERE NOT is_current) AS prior_share,
               MAX(share) FILTER (WHERE is_current) AS current_share,
               MAX(market) FILTER (WHERE NOT is_current) AS prior_market,
               MAX(market) FILTER (WHERE is_current) AS current_market
        FROM windowed GROUP BY 1 ORDER BY 3 - 2
        """
    ).fetchall()
    gainers = [row for row in esl if row[2] > row[1]]
    print(f"\n  ESLs gaining share (trailing 4q vs prior 4q): {len(gainers)} of {len(esl)}")
    for name, prior, current, prior_market, current_market in esl:
        if name in ("ORTHOPEDICS", "GENERAL SURGERY", "GASTROENTEROLOGY"):
            direction = "grows" if current_market > prior_market else "shrinks"
            print(
                f"    {name:<20} {prior:5.1f}% -> {current:5.1f}% "
                f"({current - prior:+.1f}pp), market {direction} "
                f"{prior_market} -> {current_market}"
            )


if __name__ == "__main__":
    main()
