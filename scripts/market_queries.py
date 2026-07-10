"""The nine market-share queries behind the Las Vegas report.

Every derivation lives here, in SQL: the leader gap, its quarter-over-quarter
trend, category and service-line shares, year-over-year deltas, sort order, and
the quarter labels. The browser runtime draws and formats; it never computes. If
you find yourself wanting arithmetic in `charts_v1.js`, add a column here instead.

Window conventions, all bounds on quarter starts so the compiler can rewrite them
to `DATE_TRUNC('quarter', __REPORT_DATE__) - INTERVAL n MONTH`:

* ``UPPER``  -- the start of the quarter *containing* the report date, exclusive,
  so a partial quarter never lands in a report.
* ``RACE``   -- the 16 complete quarters below ``UPPER``.
* ``LAST8``  -- the 8 complete quarters below ``UPPER``.
* ``CURRENT`` / ``PRIOR`` -- trailing 4 complete quarters, and the 4 before those.
"""

from __future__ import annotations

# Anchored at 2025-06-30. The caller confirms each of these as `relative_quarter`.
UPPER = "DATE '2025-04-01'"
RACE_START = "DATE '2021-04-01'"  # UPPER - 16 quarters
LAST8_START = "DATE '2023-04-01'"  # UPPER - 8 quarters
CURRENT_START = "DATE '2024-04-01'"  # UPPER - 4 quarters

QUARTER_LITERALS = ["2021-04-01", "2023-04-01", "2024-04-01", "2025-04-01"]

# 'Q' || 1 || '''' || '25'  ->  Q1'25
_QTR_LABEL = (
    "'Q' || quarter({col}) || '''' || strftime({col}, '%y')"
)

# The six service lines whose quarterly trajectory the report charts.
TRAJECTORY_ESLS = [
    "GENERAL SURGERY",
    "GASTROENTEROLOGY",
    "ONCOLOGY (MEDICAL)",
    "GYNECOLOGY",
    "ORTHOPEDICS",
    "CARDIAC (MEDICAL)",
]
_TRAJECTORY_LIST = ", ".join(f"'{esl}'" for esl in TRAJECTORY_ESLS)


RACE_QUARTERS = f"""
WITH quarterly AS (
  SELECT period_quarter,
         SUM(cases) FILTER (WHERE is_hca) AS hca,
         SUM(cases) FILTER (WHERE health_system = 'Universal Health Services') AS uhs
  FROM marketshare_volume
  WHERE period_quarter >= {RACE_START} AND period_quarter < {UPPER}
  GROUP BY period_quarter
)
SELECT {_QTR_LABEL.format(col='period_quarter')} AS qtr,
       hca, uhs,
       uhs - hca AS gap,
       COALESCE(
         (uhs - hca) - LAG(uhs - hca) OVER (ORDER BY period_quarter), 0
       ) AS gap_trend
FROM quarterly
ORDER BY period_quarter
""".strip()


KPI_SUMMARY = f"""
WITH quarterly AS (
  SELECT period_quarter,
         SUM(cases) FILTER (WHERE is_hca) AS hca,
         SUM(cases) FILTER (WHERE health_system = 'Universal Health Services') AS uhs
  FROM marketshare_volume
  WHERE period_quarter >= {RACE_START} AND period_quarter < {UPPER}
  GROUP BY period_quarter
),
bounds AS (
  SELECT MIN(period_quarter) AS first_q, MAX(period_quarter) AS last_q FROM quarterly
),
gaps AS (
  SELECT
    (SELECT uhs - hca FROM quarterly WHERE period_quarter = (SELECT last_q FROM bounds))
      AS gap_now,
    (SELECT uhs - hca FROM quarterly WHERE period_quarter = (SELECT first_q FROM bounds))
      AS gap_then,
    (SELECT {_QTR_LABEL.format(col='last_q')} FROM bounds) AS last_qtr,
    (SELECT {_QTR_LABEL.format(col='first_q')} FROM bounds) AS first_qtr
),
current_year AS (
  SELECT
    ROUND(SUM(cases) FILTER (WHERE is_hca) * 100.0 / SUM(cases), 1) AS hca_share_pct,
    ROUND(SUM(cases) FILTER (WHERE health_system = 'Universal Health Services')
          * 100.0 / SUM(cases), 1) AS uhs_share_pct,
    ROUND(SUM(cases) FILTER (WHERE is_hca AND category = 'ER Admissions') * 100.0
          / SUM(cases) FILTER (WHERE category = 'ER Admissions'), 1) AS er_share_pct
  FROM marketshare_volume
  WHERE period_quarter >= {CURRENT_START} AND period_quarter < {UPPER}
),
esl_windows AS (
  SELECT esl_level_2 AS esl,
         period_quarter >= {CURRENT_START} AS is_current,
         SUM(cases) FILTER (WHERE is_hca) * 100.0 / SUM(cases) AS share,
         SUM(cases) FILTER (WHERE is_hca) AS hca_cases
  FROM marketshare_volume
  WHERE esl_level_2 IS NOT NULL
    AND period_quarter >= {LAST8_START} AND period_quarter < {UPPER}
  GROUP BY 1, 2
),
esl_change AS (
  SELECT esl,
         MAX(share) FILTER (WHERE is_current) - MAX(share) FILTER (WHERE NOT is_current)
           AS share_change_pp,
         MAX(hca_cases) FILTER (WHERE is_current)
           - MAX(hca_cases) FILTER (WHERE NOT is_current) AS vol_change
  FROM esl_windows GROUP BY esl
),
esl_rollup AS (
  SELECT COUNT(*) FILTER (WHERE share_change_pp > 0) AS esl_gainers,
         COUNT(*) AS esl_total,
         ROUND(MAX(share_change_pp) FILTER (WHERE esl = 'ORTHOPEDICS'), 1)
           AS ortho_change_pp
  FROM esl_change
),
surgical_volume AS (
  SELECT SUM(cases) FILTER (WHERE period_quarter >= {CURRENT_START})
         - SUM(cases) FILTER (WHERE period_quarter < {CURRENT_START})
           AS surgical_vol_change
  FROM marketshare_volume
  WHERE is_hca AND category = 'Surgical'
    AND period_quarter >= {LAST8_START} AND period_quarter < {UPPER}
)
SELECT gaps.gap_now, gaps.gap_then, gaps.first_qtr, gaps.last_qtr,
       current_year.hca_share_pct, current_year.uhs_share_pct,
       current_year.er_share_pct,
       esl_rollup.esl_gainers, esl_rollup.esl_total, esl_rollup.ortho_change_pp,
       surgical_volume.surgical_vol_change
FROM gaps, current_year, esl_rollup, surgical_volume
""".strip()


CATEGORY_SHARE_QUARTERS = f"""
SELECT {_QTR_LABEL.format(col='period_quarter')} AS qtr,
       ROUND(SUM(cases) FILTER (WHERE is_hca AND category = 'ER Admissions') * 100.0
             / SUM(cases) FILTER (WHERE category = 'ER Admissions'), 1) AS er,
       ROUND(SUM(cases) FILTER (WHERE is_hca AND category = 'Surgical') * 100.0
             / SUM(cases) FILTER (WHERE category = 'Surgical'), 1) AS surgical,
       ROUND(SUM(cases) FILTER (WHERE is_hca AND category = 'Medical') * 100.0
             / SUM(cases) FILTER (WHERE category = 'Medical'), 1) AS medical
FROM marketshare_volume
WHERE period_quarter >= {LAST8_START} AND period_quarter < {UPPER}
GROUP BY period_quarter
ORDER BY period_quarter
""".strip()


CATEGORY_DETAIL = f"""
SELECT {_QTR_LABEL.format(col='period_quarter')} AS qtr,
       category,
       SUM(cases) AS market_cases,
       SUM(cases) FILTER (WHERE is_hca) AS hca_cases,
       ROUND(SUM(cases) FILTER (WHERE is_hca) * 100.0 / SUM(cases), 1) AS share_pct
FROM marketshare_volume
WHERE period_quarter >= {LAST8_START} AND period_quarter < {UPPER}
GROUP BY period_quarter, category
ORDER BY period_quarter, category
""".strip()


CATEGORY_SHARE_CHANGE = f"""
WITH windowed AS (
  SELECT category,
         period_quarter >= {CURRENT_START} AS is_current,
         SUM(cases) FILTER (WHERE is_hca) * 100.0 / SUM(cases) AS share
  FROM marketshare_volume
  WHERE period_quarter >= {LAST8_START} AND period_quarter < {UPPER}
  GROUP BY 1, 2
)
SELECT category,
       ROUND(MAX(share) FILTER (WHERE NOT is_current), 1) AS prior_share,
       ROUND(MAX(share) FILTER (WHERE is_current), 1) AS current_share,
       ROUND(MAX(share) FILTER (WHERE is_current)
             - MAX(share) FILTER (WHERE NOT is_current), 1) AS share_change_pp
FROM windowed
GROUP BY category
ORDER BY share_change_pp DESC, category
""".strip()


COMPETITOR_YOY = f"""
WITH windowed AS (
  SELECT health_system,
         SUM(cases) FILTER (WHERE period_quarter >= {CURRENT_START}) AS current_cases,
         SUM(cases) FILTER (WHERE period_quarter < {CURRENT_START}) AS prior_cases
  FROM marketshare_volume
  WHERE period_quarter >= {LAST8_START} AND period_quarter < {UPPER}
  GROUP BY health_system
),
totals AS (SELECT SUM(current_cases) AS market FROM windowed)
SELECT w.health_system,
       COALESCE(w.prior_cases, 0) AS prior_cases,
       COALESCE(w.current_cases, 0) AS current_cases,
       COALESCE(w.current_cases, 0) - COALESCE(w.prior_cases, 0) AS vol_change,
       CASE WHEN COALESCE(w.prior_cases, 0) = 0 THEN NULL
            ELSE ROUND((w.current_cases - w.prior_cases) * 100.0 / w.prior_cases, 1)
       END AS pct_change,
       ROUND(COALESCE(w.current_cases, 0) * 100.0 / t.market, 1) AS share_pct,
       ROUND(COALESCE(w.current_cases, 0) * 100.0 / t.market, 1) || '% ('
         || COALESCE(w.current_cases, 0) || ')' AS share_display
FROM windowed w, totals t
ORDER BY current_cases DESC, health_system
""".strip()


ESL_SUMMARY = f"""
WITH windowed AS (
  SELECT esl_level_2 AS esl,
         period_quarter >= {CURRENT_START} AS is_current,
         SUM(cases) AS market_cases,
         SUM(cases) FILTER (WHERE is_hca) AS hca_cases,
         SUM(cases) FILTER (WHERE is_hca) * 100.0 / SUM(cases) AS share
  FROM marketshare_volume
  WHERE esl_level_2 IS NOT NULL
    AND period_quarter >= {LAST8_START} AND period_quarter < {UPPER}
  GROUP BY 1, 2
)
SELECT esl,
       MAX(market_cases) FILTER (WHERE NOT is_current) AS prior_market,
       MAX(hca_cases) FILTER (WHERE NOT is_current) AS prior_hca,
       ROUND(MAX(share) FILTER (WHERE NOT is_current), 1) AS prior_share,
       MAX(market_cases) FILTER (WHERE is_current) AS current_market,
       MAX(hca_cases) FILTER (WHERE is_current) AS current_hca,
       ROUND(MAX(share) FILTER (WHERE is_current), 1) AS current_share,
       ROUND(MAX(share) FILTER (WHERE is_current)
             - MAX(share) FILTER (WHERE NOT is_current), 1) AS share_change_pp,
       MAX(hca_cases) FILTER (WHERE is_current)
         - MAX(hca_cases) FILTER (WHERE NOT is_current) AS vol_change
FROM windowed
GROUP BY esl
ORDER BY vol_change DESC, esl
""".strip()


# Same numbers as ESL_SUMMARY, ordered for the diverging bar chart. The chart
# draws rows in the order it receives them, so the sort belongs in SQL.
ESL_SHARE_CHANGE = f"""
WITH windowed AS (
  SELECT esl_level_2 AS esl,
         period_quarter >= {CURRENT_START} AS is_current,
         SUM(cases) FILTER (WHERE is_hca) AS hca_cases,
         SUM(cases) FILTER (WHERE is_hca) * 100.0 / SUM(cases) AS share
  FROM marketshare_volume
  WHERE esl_level_2 IS NOT NULL
    AND period_quarter >= {LAST8_START} AND period_quarter < {UPPER}
  GROUP BY 1, 2
),
changed AS (
  SELECT esl,
         ROUND(MAX(share) FILTER (WHERE is_current)
               - MAX(share) FILTER (WHERE NOT is_current), 1) AS share_change_pp,
         CAST(ROUND(MAX(share) FILTER (WHERE is_current), 0) AS INTEGER)
           AS current_share,
         MAX(hca_cases) FILTER (WHERE is_current)
           - MAX(hca_cases) FILTER (WHERE NOT is_current) AS vol_change
  FROM windowed GROUP BY esl
)
SELECT esl, share_change_pp, vol_change,
       CASE WHEN share_change_pp >= 0 THEN '+' ELSE '' END
         || share_change_pp || 'pp -> ' || current_share || '%' AS share_display,
       CASE WHEN vol_change >= 0 THEN '+' ELSE '' END || vol_change AS vol_display
FROM changed
ORDER BY share_change_pp DESC, esl
""".strip()


ESL_QUARTERS = f"""
SELECT esl_level_2 AS esl,
       {_QTR_LABEL.format(col='period_quarter')} AS qtr,
       ROUND(SUM(cases) FILTER (WHERE is_hca) * 100.0 / SUM(cases), 1) AS share_pct
FROM marketshare_volume
WHERE esl_level_2 IN ({_TRAJECTORY_LIST})
  AND period_quarter >= {LAST8_START} AND period_quarter < {UPPER}
GROUP BY esl_level_2, period_quarter
ORDER BY esl_level_2, period_quarter
""".strip()


QUERIES = [
    ("race_quarters", RACE_QUARTERS),
    ("kpi_summary", KPI_SUMMARY),
    ("category_share_quarters", CATEGORY_SHARE_QUARTERS),
    ("category_detail", CATEGORY_DETAIL),
    ("category_share_change", CATEGORY_SHARE_CHANGE),
    ("competitor_yoy", COMPETITOR_YOY),
    ("esl_summary", ESL_SUMMARY),
    ("esl_share_change", ESL_SHARE_CHANGE),
    ("esl_quarters", ESL_QUARTERS),
]

TEMPORAL_CONFIRMATIONS = [
    {"literal": literal, "treatment": "relative_quarter"}
    for literal in QUARTER_LITERALS
]
