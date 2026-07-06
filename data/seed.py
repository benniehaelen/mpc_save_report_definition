"""Create and populate the local DuckDB database with deterministic synthetic data.

Run: python data/seed.py

Produces data/poc.duckdb with 120 days of healthcare-flavored data ending on the
fixed anchor date of 2025-06-30, plus the empty platform tables the server needs.
The script is idempotent: it drops and recreates everything on each run.
"""

from __future__ import annotations

import datetime as dt
import random
from pathlib import Path

import duckdb

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


def _drop_and_create(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("DROP TABLE IF EXISTS facilities")
    con.execute("DROP TABLE IF EXISTS admissions")
    con.execute("DROP TABLE IF EXISTS daily_census")
    con.execute("DROP TABLE IF EXISTS tool_call_log")
    con.execute("DROP TABLE IF EXISTS report_definitions")

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
        CREATE TABLE tool_call_log (
          call_id         INTEGER PRIMARY KEY,
          conversation_id VARCHAR,
          tool_name       VARCHAR,
          sql_text        VARCHAR,
          result_name     VARCHAR,
          row_count       INTEGER,
          called_at       TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE report_definitions (
          report_id          VARCHAR,
          definition_version INTEGER,
          report_name        VARCHAR,
          definition_json    VARCHAR,
          created_at         TIMESTAMP,
          parity_attempts    INTEGER,
          PRIMARY KEY (report_id, definition_version)
        )
        """
    )


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


def main() -> None:
    rng = random.Random()
    rng.seed(42)

    if DB_PATH.exists():
        DB_PATH.unlink()

    con = duckdb.connect(str(DB_PATH))
    try:
        _drop_and_create(con)
        facilities, admissions, census = _build_rows(rng)
        con.executemany("INSERT INTO facilities VALUES (?, ?, ?, ?)", facilities)
        con.executemany("INSERT INTO admissions VALUES (?, ?, ?, ?, ?)", admissions)
        con.executemany("INSERT INTO daily_census VALUES (?, ?, ?)", census)
        print(f"Seeded {DB_PATH}")
        print(f"  facilities:   {len(facilities)}")
        print(f"  admissions:   {len(admissions)}")
        print(f"  daily_census: {len(census)}")
        print(f"  anchor date:  {ANCHOR_DATE.isoformat()}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
