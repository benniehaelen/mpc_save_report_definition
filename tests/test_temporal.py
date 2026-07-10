"""Unit tests for temporal re-parameterization, the riskiest piece."""

from __future__ import annotations

from server import temporal

ANCHOR = "2025-06-30"


def test_detects_and_rewrites_relative_literal():
    sql = "WHERE admit_date >= DATE '2025-06-01'"
    out, warnings = temporal.reparameterize(sql, ANCHOR)
    assert "__REPORT_DATE__ - INTERVAL 29 DAY" in out
    assert "DATE '2025-06-01'" not in out
    assert warnings == []


def test_full_window_example_from_brief():
    sql = (
        "admit_date >= DATE '2025-06-01' "
        "AND admit_date < DATE '2025-07-01'"
    )
    out, _ = temporal.reparameterize(sql, ANCHOR)
    assert out == (
        "admit_date >= __REPORT_DATE__ - INTERVAL 29 DAY "
        "AND admit_date < __REPORT_DATE__ + INTERVAL 1 DAY"
    )


def test_anchor_itself_becomes_bare_token():
    sql = "WHERE census_date = DATE '2025-06-30'"
    out, _ = temporal.reparameterize(sql, ANCHOR)
    assert "= __REPORT_DATE__" in out
    assert "INTERVAL" not in out


def test_bare_quoted_date_is_handled():
    sql = "WHERE admit_date >= '2025-06-15'"
    out, _ = temporal.reparameterize(sql, ANCHOR)
    assert "__REPORT_DATE__ - INTERVAL 15 DAY" in out


def test_fixed_confirmation_passthrough():
    sql = "WHERE admit_date >= DATE '2025-06-01'"
    confirmations = [{"literal": "2025-06-01", "treatment": "fixed"}]
    out, warnings = temporal.reparameterize(sql, ANCHOR, confirmations)
    assert "DATE '2025-06-01'" in out
    assert "__REPORT_DATE__" not in out
    assert any("fixed" in w for w in warnings)


def test_conservative_fallback_for_distant_literal():
    sql = "WHERE admit_date >= DATE '2020-01-01'"
    out, warnings = temporal.reparameterize(sql, ANCHOR)
    assert "DATE '2020-01-01'" in out
    assert "__REPORT_DATE__" not in out
    assert any("left fixed" in w for w in warnings)


def test_bind_report_date_substitutes_token():
    sql = "admit_date >= __REPORT_DATE__ - INTERVAL 29 DAY"
    bound = temporal.bind_report_date(sql, "2025-05-15")
    assert bound == "admit_date >= DATE '2025-05-15' - INTERVAL 29 DAY"


# --- quarter grain -------------------------------------------------------
#
# A quarter is not a fixed number of days, so a quarter boundary rewritten as a
# day offset drifts off the boundary as the report date moves.


def test_confirmed_quarter_literal_rewrites_at_quarter_grain():
    sql = "WHERE period_quarter >= DATE '2021-04-01'"
    out, warnings = temporal.reparameterize(
        sql, ANCHOR, [{"literal": "2021-04-01", "treatment": "relative_quarter"}]
    )
    assert out == (
        "WHERE period_quarter >= DATE_TRUNC('quarter', __REPORT_DATE__) "
        "- INTERVAL 48 MONTH"
    )
    assert warnings == []


def test_anchor_quarter_becomes_a_bare_date_trunc():
    sql = "WHERE period_quarter < DATE '2025-04-01'"
    out, _warnings = temporal.reparameterize(
        sql, ANCHOR, [{"literal": "2025-04-01", "treatment": "relative_quarter"}]
    )
    assert out == "WHERE period_quarter < DATE_TRUNC('quarter', __REPORT_DATE__)"
    assert "INTERVAL" not in out


def test_unconfirmed_past_quarter_start_within_a_year_uses_quarter_grain():
    sql = "WHERE period_quarter >= DATE '2025-01-01'"
    out, warnings = temporal.reparameterize(sql, ANCHOR)
    assert "DATE_TRUNC('quarter', __REPORT_DATE__) - INTERVAL 3 MONTH" in out
    assert warnings == []


def test_unconfirmed_future_quarter_start_still_uses_a_day_offset():
    """`< DATE '2025-07-01'` is an exclusive upper bound written as the day after
    the anchor; the day form reproduces it exactly, so leave it alone."""
    sql = "WHERE admit_date < DATE '2025-07-01'"
    out, _warnings = temporal.reparameterize(sql, ANCHOR)
    assert out == "WHERE admit_date < __REPORT_DATE__ + INTERVAL 1 DAY"


def test_non_quarter_start_within_a_year_still_uses_a_day_offset():
    sql = "WHERE d >= DATE '2025-05-01'"
    out, _warnings = temporal.reparameterize(sql, ANCHOR)
    assert out == "WHERE d >= __REPORT_DATE__ - INTERVAL 60 DAY"


def test_distant_unconfirmed_quarter_literal_stays_fixed_with_a_warning():
    sql = "WHERE period_quarter >= DATE '2020-04-01'"  # 20 quarters back
    out, warnings = temporal.reparameterize(sql, ANCHOR)
    assert "DATE '2020-04-01'" in out
    assert "DATE_TRUNC" not in out
    assert any("left fixed" in w for w in warnings)


def test_quarter_literal_beyond_the_cap_stays_fixed_even_when_confirmed():
    sql = "WHERE period_quarter >= DATE '2014-01-01'"  # 45 quarters back
    out, warnings = temporal.reparameterize(
        sql, ANCHOR, [{"literal": "2014-01-01", "treatment": "relative_quarter"}]
    )
    assert "DATE '2014-01-01'" in out
    assert any("quarters from the anchor" in w for w in warnings)


def test_relative_quarter_confirmation_on_a_non_quarter_date_falls_back_and_warns():
    sql = "WHERE d >= DATE '2025-05-15'"
    out, warnings = temporal.reparameterize(
        sql, ANCHOR, [{"literal": "2025-05-15", "treatment": "relative_quarter"}]
    )
    assert out == "WHERE d >= __REPORT_DATE__ - INTERVAL 46 DAY"
    assert any("not a quarter start" in w for w in warnings)


def test_quarter_rewrite_binds_and_executes_back_to_the_original_literal():
    """The whole point: rewrite -> bind -> DuckDB must return the source date."""
    import duckdb

    con = duckdb.connect()
    for literal, quarters_back in (("2021-04-01", 16), ("2023-04-01", 8), ("2025-04-01", 0)):
        sql = f"SELECT DATE '{literal}' AS d"
        rewritten, _warnings = temporal.reparameterize(
            sql, ANCHOR, [{"literal": literal, "treatment": "relative_quarter"}]
        )
        assert f"INTERVAL {3 * quarters_back} MONTH" in rewritten or quarters_back == 0
        bound = temporal.bind_report_date(rewritten, ANCHOR)
        assert "__REPORT_DATE__" not in bound
        assert con.execute(bound).fetchone()[0].date().isoformat() == literal


def test_quarter_rewrite_tracks_a_different_report_date():
    """Bound at 2025-03-31 the window slides back exactly one quarter."""
    import duckdb

    con = duckdb.connect()
    rewritten, _warnings = temporal.reparameterize(
        "SELECT DATE '2021-04-01' AS d",
        ANCHOR,
        [{"literal": "2021-04-01", "treatment": "relative_quarter"}],
    )
    bound = temporal.bind_report_date(rewritten, "2025-03-31")
    assert con.execute(bound).fetchone()[0].date().isoformat() == "2021-01-01"
