"""Distillation: turn a session record into a report definition.

This POC uses a deterministic heuristic, which is enough to prove the
architecture. The real system uses an LLM here; the Distiller protocol below
makes that swap trivial (one distill method).

Heuristic rules:
  1. Parse the final artifact. Every <table data-result="..."> and every element
     with a data-value attribute names a result.
  2. Match those names against result_name values in the tool-call log. Only
     matched queries enter the definition, so superseded and dead-end queries
     drop out because nothing in the artifact references them.
  3. Pass each surviving query through temporal.reparameterize.
  4. Convert the artifact into a Jinja2 template: replace each matched table's
     body rows with a for-loop over its named result, and replace data-value
     numbers with template expressions. Anything that cannot be templated is
     reported as an unreplayable section rather than silently kept.
"""

from __future__ import annotations

import re
from typing import Protocol

from server import temporal

# Owner identity is out of scope for this POC.

_TABLE_RE = re.compile(
    r'<table\b[^>]*\bdata-result="([^"]+)"[^>]*>(.*?)</table>',
    re.IGNORECASE | re.DOTALL,
)
_TBODY_RE = re.compile(r"<tbody\b[^>]*>.*?</tbody>", re.IGNORECASE | re.DOTALL)
_DATA_VALUE_RE = re.compile(
    r'(<(\w+)\b[^>]*\bdata-value="([^".]+)\.([^"]+)"[^>]*>)(.*?)(</\2>)',
    re.IGNORECASE | re.DOTALL,
)
_DATA_RESULT_NAMES = re.compile(r'data-result="([^"]+)"', re.IGNORECASE)
_DATA_VALUE_REFS = re.compile(r'data-value="([^"]+)"', re.IGNORECASE)


class Distiller(Protocol):
    """One-method interface so an LLM distiller can replace the heuristic."""

    def distill(self, session_record: dict) -> dict: ...


def _latest_by_name(log_rows: list[dict]) -> dict[str, str]:
    """Map each result_name to its most recent SQL text (later calls win)."""
    latest: dict[str, str] = {}
    for row in log_rows:
        name = row.get("result_name")
        if name:
            latest[name] = row["sql_text"]
    return latest


def _loop_for(name: str) -> str:
    return (
        "<tbody>\n"
        "{% for __row in " + name + ".rows %}"
        "<tr>{% for __col in " + name + ".columns %}"
        "<td>{{ __row[__col] }}</td>{% endfor %}</tr>\n"
        "{% endfor %}</tbody>"
    )


def _build_template(
    html: str,
    matched: set[str],
    unreplayable: list[str],
) -> str:
    """Rewrite matched tables and data-value spans into Jinja constructs."""

    def _table_sub(match: re.Match[str]) -> str:
        name = match.group(1)
        inner = match.group(2)
        whole = match.group(0)
        if name not in matched:
            return whole
        if not _TBODY_RE.search(inner):
            unreplayable.append(
                f"table '{name}' has no <tbody> to templatize; left static"
            )
            return whole
        new_inner = _TBODY_RE.sub(lambda _m: _loop_for(name), inner, count=1)
        return whole.replace(inner, new_inner, 1)

    def _value_sub(match: re.Match[str]) -> str:
        open_tag, _tag, result_name, field, _text, close_tag = match.groups()
        if result_name not in matched:
            unreplayable.append(
                f"value '{result_name}.{field}' has no matching query; left static"
            )
            return match.group(0)
        expr = "{{ " + result_name + ".rows[0]['" + field + "'] }}"
        return f"{open_tag}{expr}{close_tag}"

    templated = _TABLE_RE.sub(_table_sub, html)
    templated = _DATA_VALUE_RE.sub(_value_sub, templated)
    return templated


def distill(
    report_name: str,
    transcript: list[dict],
    final_artifact: dict,
    log_rows: list[dict],
    anchor_date: str,
    temporal_confirmations: list[dict] | None = None,
    attempt: int = 1,
) -> dict:
    """Produce a candidate definition from the session record.

    On retry attempts (attempt > 1) the matching is widened to include every
    logged query, not just those referenced by the artifact.
    """
    html = final_artifact.get("content", "")
    latest = _latest_by_name(log_rows)

    referenced: list[str] = []
    for name in _DATA_RESULT_NAMES.findall(html):
        if name not in referenced:
            referenced.append(name)
    for ref in _DATA_VALUE_REFS.findall(html):
        name = ref.split(".", 1)[0]
        if name not in referenced:
            referenced.append(name)

    unreplayable: list[str] = []
    for name in referenced:
        if name not in latest:
            unreplayable.append(
                f"reference '{name}' has no matching query in the tool-call log"
            )

    included = [name for name in referenced if name in latest]
    if attempt > 1:
        for name in latest:
            if name not in included:
                included.append(name)

    matched = set(included)
    warnings: list[str] = []
    queries: list[dict] = []
    for name in included:
        new_sql, sql_warnings = temporal.reparameterize(
            latest[name], anchor_date, temporal_confirmations
        )
        warnings.extend(sql_warnings)
        queries.append({"result_name": name, "sql": new_sql})

    template = _build_template(html, matched, unreplayable)

    return {
        "report_name": report_name,
        "title": final_artifact.get("title", report_name),
        "anchor_date": anchor_date,
        "queries": queries,
        "template": template,
        "warnings": warnings,
        "unreplayable_sections": unreplayable,
    }
