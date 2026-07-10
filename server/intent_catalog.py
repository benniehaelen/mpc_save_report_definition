"""Keyword intent catalog for the nl_query tool.

A tiny stand-in for the resolver pipeline: match keywords in a question to a
suggested SQL template. Every template uses literal dates for the last 30 days
ending at the anchor date (2025-06-01 through 2025-06-30 inclusive). Intents are
checked in order and the first full keyword match wins, so more specific intents
are listed first.
"""

from __future__ import annotations

# Date window baked into the suggestions: the last 30 days ending at the anchor.
_WINDOW = "DATE '2025-06-01' AND {col} < DATE '2025-07-01'"

# Market-share suggestions are quarter-windowed. The bounds are quarter starts, so
# the caller confirms them as `relative_quarter` at save time and the compiler
# rewrites them to DATE_TRUNC('quarter', __REPORT_DATE__) - INTERVAL n MONTH.
# The upper bound is the start of the quarter *containing* the report date, so a
# partial quarter never lands in a report.
_MARKET_SHARE_RACE = (
    "SELECT period_quarter,\n"
    "       'Q' || quarter(period_quarter) || ''''\n"
    "         || strftime(period_quarter, '%y') AS qtr,\n"
    "       SUM(cases) FILTER (WHERE is_hca) AS hca_cases,\n"
    "       SUM(cases) FILTER (WHERE health_system = 'Universal Health Services')\n"
    "         AS uhs_cases\n"
    "FROM marketshare_volume\n"
    "WHERE period_quarter >= DATE '2021-04-01'\n"
    "  AND period_quarter < DATE '2025-04-01'\n"
    "GROUP BY period_quarter\n"
    "ORDER BY period_quarter"
)

_ESL_SHARE = (
    "SELECT esl_level_2 AS esl,\n"
    "       SUM(cases) FILTER (WHERE is_hca) AS hca_cases,\n"
    "       SUM(cases) AS market_cases,\n"
    "       ROUND(SUM(cases) FILTER (WHERE is_hca) * 100.0 / SUM(cases), 1)\n"
    "         AS share_pct\n"
    "FROM marketshare_volume\n"
    "WHERE esl_level_2 IS NOT NULL\n"
    "  AND period_quarter >= DATE '2023-04-01'\n"
    "  AND period_quarter < DATE '2025-04-01'\n"
    "GROUP BY esl_level_2\n"
    "ORDER BY share_pct DESC"
)

_ADMISSIONS_BY_DIVISION = (
    "SELECT f.division, COUNT(*) AS admissions\n"
    "FROM admissions a JOIN facilities f ON a.facility_id = f.facility_id\n"
    "WHERE a.admit_date >= DATE '2025-06-01' AND a.admit_date < DATE '2025-07-01'\n"
    "GROUP BY f.division\n"
    "ORDER BY f.division"
)

_ADMISSIONS_BY_SERVICE_LINE = (
    "SELECT service_line, COUNT(*) AS admissions\n"
    "FROM admissions\n"
    "WHERE admit_date >= DATE '2025-06-01' AND admit_date < DATE '2025-07-01'\n"
    "GROUP BY service_line\n"
    "ORDER BY service_line"
)

_CENSUS_OCCUPANCY = (
    "SELECT f.facility_name,\n"
    "       ROUND(AVG(c.midnight_census), 1) AS avg_census,\n"
    "       ROUND(AVG(c.midnight_census * 1.0 / f.bed_count), 4) AS occupancy_rate\n"
    "FROM daily_census c JOIN facilities f ON c.facility_id = f.facility_id\n"
    "WHERE c.census_date >= DATE '2025-06-01' AND c.census_date < DATE '2025-07-01'\n"
    "GROUP BY f.facility_name\n"
    "ORDER BY f.facility_name"
)

_LENGTH_OF_STAY = (
    "SELECT f.division, ROUND(AVG(a.length_of_stay), 2) AS avg_los\n"
    "FROM admissions a JOIN facilities f ON a.facility_id = f.facility_id\n"
    "WHERE a.admit_date >= DATE '2025-06-01' AND a.admit_date < DATE '2025-07-01'\n"
    "GROUP BY f.division\n"
    "ORDER BY f.division"
)

_DAILY_TREND = (
    "SELECT admit_date, COUNT(*) AS admissions\n"
    "FROM admissions\n"
    "WHERE admit_date >= DATE '2025-06-01' AND admit_date < DATE '2025-07-01'\n"
    "GROUP BY admit_date\n"
    "ORDER BY admit_date"
)

# Each intent: name, keywords (ALL must be substrings of the question), the
# suggested SQL, and the tables the caller will likely need. Order matters.
INTENTS: list[dict] = [
    # Market-share intents come FIRST, and not for taste. `match()` returns the
    # first intent whose every keyword is a substring, and "los" (length of stay)
    # is a substring of "losing" -- so "which service lines is HCA losing share
    # in?" resolves to length_of_stay_by_division if these sit at the bottom.
    {
        "name": "esl_share",
        "keywords": ["esl"],
        "sql": _ESL_SHARE,
        "tables": ["marketshare_volume"],
    },
    {
        # Both keywords required, so "admissions by service line" still routes to
        # admissions_by_service_line below.
        "name": "esl_share",
        "keywords": ["service line", "share"],
        "sql": _ESL_SHARE,
        "tables": ["marketshare_volume"],
    },
    {
        "name": "market_share_race",
        "keywords": ["market share"],
        "sql": _MARKET_SHARE_RACE,
        "tables": ["marketshare_volume"],
    },
    {
        "name": "market_share_race",
        "keywords": ["gap"],
        "sql": _MARKET_SHARE_RACE,
        "tables": ["marketshare_volume"],
    },
    {
        "name": "market_share_race",
        "keywords": ["race"],
        "sql": _MARKET_SHARE_RACE,
        "tables": ["marketshare_volume"],
    },
    {
        "name": "market_share_race",
        "keywords": ["competitor"],
        "sql": _MARKET_SHARE_RACE,
        "tables": ["marketshare_volume"],
    },
    {
        "name": "admissions_by_division",
        "keywords": ["admission", "division"],
        "sql": _ADMISSIONS_BY_DIVISION,
        "tables": ["admissions", "facilities"],
    },
    {
        "name": "admissions_by_service_line",
        "keywords": ["admission", "service"],
        "sql": _ADMISSIONS_BY_SERVICE_LINE,
        "tables": ["admissions"],
    },
    {
        "name": "census_occupancy_by_facility",
        "keywords": ["census"],
        "sql": _CENSUS_OCCUPANCY,
        "tables": ["daily_census", "facilities"],
    },
    {
        "name": "census_occupancy_by_facility",
        "keywords": ["occupancy"],
        "sql": _CENSUS_OCCUPANCY,
        "tables": ["daily_census", "facilities"],
    },
    {
        "name": "length_of_stay_by_division",
        "keywords": ["length of stay"],
        "sql": _LENGTH_OF_STAY,
        "tables": ["admissions", "facilities"],
    },
    {
        "name": "length_of_stay_by_division",
        "keywords": ["los"],
        "sql": _LENGTH_OF_STAY,
        "tables": ["admissions", "facilities"],
    },
    {
        "name": "daily_admissions_trend",
        "keywords": ["trend"],
        "sql": _DAILY_TREND,
        "tables": ["admissions"],
    },
    {
        "name": "daily_admissions_trend",
        "keywords": ["daily"],
        "sql": _DAILY_TREND,
        "tables": ["admissions"],
    },
]


def match(question: str) -> dict | None:
    """Return the first intent whose keywords all appear in the question."""
    text = question.lower()
    for intent in INTENTS:
        if all(keyword in text for keyword in intent["keywords"]):
            return intent
    return None
