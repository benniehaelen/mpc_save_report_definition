"""The seeded market-share story must hold, or every downstream report lies.

These assertions are the contract `data/seed.py` promises: the reports, the
editorial watch threshold, and the end-to-end test all read these curves.
"""

from __future__ import annotations

import pytest

from server import intent_catalog
from server.db import get_connection


@pytest.fixture(scope="module")
def con():
    # Read-only, matching the rest of the suite: db.get_connection caches per
    # (path, read_only), and DuckDB refuses a second connection to the same file
    # under a different configuration.
    return get_connection()


def _quarterly_totals(con) -> list[tuple]:
    return con.execute(
        """
        SELECT period_quarter,
               SUM(cases) FILTER (WHERE is_hca) AS hca,
               SUM(cases) FILTER (WHERE health_system = 'Universal Health Services') AS uhs
        FROM marketshare_volume GROUP BY 1 ORDER BY 1
        """
    ).fetchall()


def test_twenty_quarters_ending_before_the_anchor_quarter(con):
    quarters = con.execute(
        "SELECT DISTINCT period_quarter FROM marketshare_volume ORDER BY 1"
    ).fetchall()
    assert len(quarters) == 20
    assert str(quarters[0][0]) == "2020-04-01"
    # Q1'25 is the last complete quarter before the 2025-06-30 anchor.
    assert str(quarters[-1][0]) == "2025-01-01"


def test_uhs_leads_and_the_gap_narrows_monotonically(con):
    rows = _quarterly_totals(con)
    gaps = [uhs - hca for _q, hca, uhs in rows]
    assert all(uhs > hca for _q, hca, uhs in rows), "UHS must lead in every quarter"
    assert gaps == sorted(gaps, reverse=True), "the gap must narrow every quarter"
    assert 3900 <= gaps[0] <= 4100
    assert 600 <= gaps[-1] <= 800


def test_gap_straddles_the_editorial_watch_threshold(con):
    """The demo's watch is `gap_now < 800`. It must fire at one replay date and
    not the other, or the staleness banner is never exercised end to end."""
    rows = _quarterly_totals(con)
    gap_by_quarter = {str(q): uhs - hca for q, hca, uhs in rows}
    assert gap_by_quarter["2024-10-01"] >= 800  # as-of 2025-03-31 -> no banner
    assert gap_by_quarter["2025-01-01"] < 800  # as-of 2025-06-30 -> banner


def test_category_share_curves(con):
    rows = con.execute(
        """
        SELECT category, period_quarter,
               SUM(cases) FILTER (WHERE is_hca) * 100.0 / SUM(cases) AS share
        FROM marketshare_volume GROUP BY 1, 2 ORDER BY 1, 2
        """
    ).fetchall()
    by_category: dict[str, list[float]] = {}
    for category, _quarter, share in rows:
        by_category.setdefault(category, []).append(share)

    er = by_category["ER Admissions"]
    assert all(34.0 <= share <= 35.0 for share in er), "ER share is flat at ~34.5%"

    surgical = by_category["Surgical"]
    assert surgical[0] == pytest.approx(23.0, abs=0.5)
    assert surgical[-1] == pytest.approx(27.0, abs=0.5)
    assert surgical[-1] > surgical[0]

    medical = by_category["Medical"]
    assert medical[0] == pytest.approx(49.0, abs=0.5)
    assert medical[-1] == pytest.approx(45.0, abs=0.5)
    assert medical[-1] < medical[0]


def test_west_henderson_opens_late_medical_only_and_ramps(con):
    rows = con.execute(
        """
        SELECT period_quarter, SUM(cases) FROM marketshare_volume
        WHERE health_system = 'West Henderson Hospital' GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    assert len(rows) == 3, "zero cases until the final three quarters"
    assert [str(q) for q, _c in rows] == ["2024-07-01", "2024-10-01", "2025-01-01"]
    volumes = [c for _q, c in rows]
    assert volumes == sorted(volumes) and volumes[-1] > 5 * volumes[0], "rapid growth"

    categories = con.execute(
        "SELECT DISTINCT category FROM marketshare_volume "
        "WHERE health_system = 'West Henderson Hospital'"
    ).fetchall()
    assert categories == [("Medical",)]


def test_esl_is_null_only_for_er_rows(con):
    stray = con.execute(
        "SELECT COUNT(*) FROM marketshare_volume "
        "WHERE (esl_level_2 IS NULL) <> (category = 'ER Admissions')"
    ).fetchone()[0]
    assert stray == 0


def test_no_negative_or_zero_case_rows(con):
    assert con.execute(
        "SELECT COUNT(*) FROM marketshare_volume WHERE cases <= 0"
    ).fetchone()[0] == 0


def _esl_share_change(con) -> dict[str, tuple[float, float, int, int]]:
    rows = con.execute(
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
               MAX(share) FILTER (WHERE NOT is_current),
               MAX(share) FILTER (WHERE is_current),
               MAX(market) FILTER (WHERE NOT is_current),
               MAX(market) FILTER (WHERE is_current)
        FROM windowed GROUP BY 1
        """
    ).fetchall()
    return {name: (prior, current, pm, cm) for name, prior, current, pm, cm in rows}


def test_hca_gains_share_in_twelve_of_seventeen_service_lines(con):
    changes = _esl_share_change(con)
    assert len(changes) == 17
    gainers = [esl for esl, (p, c, _pm, _cm) in changes.items() if c > p]
    assert len(gainers) == 12
    # The six the brief calls out by name must be among them.
    for esl in (
        "GENERAL SURGERY",
        "GASTROENTEROLOGY",
        "ONCOLOGY (MEDICAL)",
        "CARDIAC (MEDICAL)",
        "THORACIC SURGERY",
        "GYNECOLOGY",
    ):
        assert esl in gainers, f"{esl} should gain share"


def test_orthopedics_loses_share_while_its_market_grows(con):
    prior, current, prior_market, current_market = _esl_share_change(con)["ORTHOPEDICS"]
    assert -3.0 <= current - prior <= -2.0, "orthopedics loses 2-3pp"
    assert current_market > prior_market, "and it loses it in a growing market"


def test_knowledge_graph_covers_the_marketshare_columns(con):
    metrics = {row[0] for row in con.execute("SELECT metric_id FROM metrics").fetchall()}
    assert {"cases", "market_cases", "hca_cases", "share_pct", "gap",
            "gap_trend", "share_change_pp", "vol_change"} <= metrics

    dimensions = dict(
        con.execute("SELECT dimension, value_set FROM dimension_value_sets").fetchall()
    )
    assert dimensions["health_system"] == "health_systems"
    assert dimensions["category"] == "categories"
    assert dimensions["esl_level_2"] == "esl_level_2"
    assert dimensions["esl"] == "esl_level_2"

    esls = con.execute(
        "SELECT COUNT(*) FROM value_sets WHERE value_set = 'esl_level_2'"
    ).fetchone()[0]
    assert esls == 17


def test_losing_share_question_no_longer_routes_to_length_of_stay():
    """'losing' contains 'los'; the market intents must be matched first."""
    assert intent_catalog.match(
        "which service lines is HCA losing share in?"
    )["name"] == "esl_share"
    assert intent_catalog.match("what is the gap to the market leader?")[
        "name"
    ] == "market_share_race"
    # ...without stealing the questions the original intents owned.
    assert intent_catalog.match("admissions by service line")[
        "name"
    ] == "admissions_by_service_line"
    assert intent_catalog.match("average length of stay by division")[
        "name"
    ] == "length_of_stay_by_division"
