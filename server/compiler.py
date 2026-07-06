"""Distillation: turn a session record into a report definition.

This POC uses a deterministic heuristic, which is enough to prove the
architecture. The real system uses an LLM here; the Distiller protocol below
makes that swap trivial (one distill method).

The distilled definition has the four parts from the design:
  - parameterized_sql: the named query set, with date literals tokenized
  - metric_bindings:   result fields bound to governed metrics and ValueSets
  - reasoning_steps:   steps the runner replays over fresh results for narrative
  - rendering_spec:    the template plus the output formats and title

Heuristic rules:
  1. Parse the final artifact. Every <table data-result="...">, every element
     with a data-value attribute, and every element with a data-reasoning
     attribute names a result.
  2. Match those names against result_name values in the tool-call log. Only
     matched queries enter the definition, so superseded and dead-end queries
     drop out because nothing in the artifact references them.
  3. Pass each surviving query through temporal.reparameterize.
  4. Bind fields (table headers and data-value fields) to catalog metrics and
     ValueSets.
  5. Convert the artifact into a Jinja2 template: table bodies become loops,
     data-value numbers become expressions, and data-reasoning elements become
     narrative expressions. Anything that cannot be templated is reported as an
     unreplayable section rather than silently kept.
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
_THEAD_RE = re.compile(r"<thead\b[^>]*>(.*?)</thead>", re.IGNORECASE | re.DOTALL)
_TH_RE = re.compile(r"<th\b[^>]*>(.*?)</th>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_DATA_VALUE_RE = re.compile(
    r'(<(\w+)\b[^>]*\bdata-value="([^".]+)\.([^"]+)"[^>]*>)(.*?)(</\2>)',
    re.IGNORECASE | re.DOTALL,
)
_DATA_REASONING_RE = re.compile(
    r'(<(\w+)\b[^>]*\bdata-reasoning="([^"]+)"[^>]*>)(.*?)(</\2>)',
    re.IGNORECASE | re.DOTALL,
)
_DATA_RESULT_NAMES = re.compile(r'data-result="([^"]+)"', re.IGNORECASE)
_DATA_VALUE_REFS = re.compile(r'data-value="([^"]+)"', re.IGNORECASE)
_OVER_ATTR = re.compile(r'data-over="([^".]+)\.([^"]+)"', re.IGNORECASE)
_AGG_ATTR = re.compile(r'data-agg="([^"]+)"', re.IGNORECASE)

_EMPTY_CATALOG = {"metrics": set(), "value_sets": {}, "dimensions": {}}


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


def _text(html: str) -> str:
    return _TAG_RE.sub("", html).strip()


def _table_headers(inner: str) -> list[str]:
    thead = _THEAD_RE.search(inner)
    if not thead:
        return []
    return [_text(th) for th in _TH_RE.findall(thead.group(1))]


def _loop_for(name: str) -> str:
    return (
        "<tbody>\n"
        "{% for __row in " + name + ".rows %}"
        "<tr>{% for __col in " + name + ".columns %}"
        "<td>{{ __row[__col] }}</td>{% endfor %}</tr>\n"
        "{% endfor %}</tbody>"
    )


def _reasoning_meta(open_tag: str) -> tuple[str, str, str] | None:
    over = _OVER_ATTR.search(open_tag)
    if not over:
        return None
    agg_match = _AGG_ATTR.search(open_tag)
    agg = agg_match.group(1) if agg_match else "max"
    return over.group(1), over.group(2), agg


def _referenced_names(html: str) -> list[str]:
    referenced: list[str] = []

    def _add(name: str) -> None:
        if name and name not in referenced:
            referenced.append(name)

    for name in _DATA_RESULT_NAMES.findall(html):
        _add(name)
    for ref in _DATA_VALUE_REFS.findall(html):
        _add(ref.split(".", 1)[0])
    for match in _DATA_REASONING_RE.finditer(html):
        meta = _reasoning_meta(match.group(1))
        if meta:
            _add(meta[0])
    return referenced


def _field_pairs(html: str, matched: set[str]) -> list[tuple[str, str]]:
    """Collect (result_name, field) pairs from table headers and data-value refs."""
    pairs: list[tuple[str, str]] = []

    def _add(result_name: str, field: str) -> None:
        pair = (result_name, field)
        if result_name in matched and field and pair not in pairs:
            pairs.append(pair)

    for name, inner in _TABLE_RE.findall(html):
        for header in _table_headers(inner):
            _add(name, header)
    for ref in _DATA_VALUE_REFS.findall(html):
        if "." in ref:
            result_name, field = ref.split(".", 1)
            _add(result_name, field)
    return pairs


def _metric_bindings(pairs: list[tuple[str, str]], catalog: dict) -> list[dict]:
    bindings: list[dict] = []
    for result_name, field in pairs:
        if field in catalog["metrics"]:
            bindings.append(
                {
                    "result_name": result_name,
                    "field": field,
                    "metric_id": field,
                    "value_set": None,
                }
            )
        elif field in catalog["dimensions"]:
            bindings.append(
                {
                    "result_name": result_name,
                    "field": field,
                    "metric_id": None,
                    "value_set": catalog["dimensions"][field],
                }
            )
    return bindings


def _build_template(
    html: str,
    matched: set[str],
    unreplayable: list[str],
) -> tuple[str, list[dict]]:
    """Rewrite tables, data-value spans, and data-reasoning elements into Jinja."""
    reasoning_steps: list[dict] = []

    def _table_sub(match: re.Match[str]) -> str:
        name, inner, whole = match.group(1), match.group(2), match.group(0)
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
        open_tag, _tag, result_name, field, _text_, close_tag = match.groups()
        if result_name not in matched:
            unreplayable.append(
                f"value '{result_name}.{field}' has no matching query; left static"
            )
            return match.group(0)
        expr = "{{ " + result_name + ".rows[0]['" + field + "'] }}"
        return f"{open_tag}{expr}{close_tag}"

    def _reasoning_sub(match: re.Match[str]) -> str:
        open_tag, _tag, step_id, _text_, close_tag = match.groups()
        meta = _reasoning_meta(open_tag)
        if meta is None:
            unreplayable.append(
                f"reasoning '{step_id}' is missing a data-over attribute; left static"
            )
            return match.group(0)
        result_name, field, agg = meta
        if result_name not in matched:
            unreplayable.append(
                f"reasoning '{step_id}' references unmatched result "
                f"'{result_name}'; left static"
            )
            return match.group(0)
        reasoning_steps.append(
            {
                "step_id": step_id,
                "result_name": result_name,
                "field": field,
                "agg": agg,
            }
        )
        expr = "{{ reasoning['" + step_id + "'] }}"
        return f"{open_tag}{expr}{close_tag}"

    templated = _TABLE_RE.sub(_table_sub, html)
    templated = _DATA_VALUE_RE.sub(_value_sub, templated)
    templated = _DATA_REASONING_RE.sub(_reasoning_sub, templated)
    return templated, reasoning_steps


def distill(
    report_name: str,
    transcript: list[dict],
    final_artifact: dict,
    log_rows: list[dict],
    anchor_date: str,
    catalog: dict | None = None,
    temporal_confirmations: list[dict] | None = None,
    attempt: int = 1,
) -> dict:
    """Produce a candidate definition from the session record.

    On retry attempts (attempt > 1) the matching is widened to include every
    logged query, not just those referenced by the artifact.
    """
    catalog = catalog or _EMPTY_CATALOG
    html = final_artifact.get("content", "")
    latest = _latest_by_name(log_rows)

    referenced = _referenced_names(html)
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
    parameterized_sql: list[dict] = []
    for name in included:
        new_sql, sql_warnings = temporal.reparameterize(
            latest[name], anchor_date, temporal_confirmations
        )
        warnings.extend(sql_warnings)
        parameterized_sql.append({"result_name": name, "sql": new_sql})

    metric_bindings = _metric_bindings(_field_pairs(html, matched), catalog)
    template, reasoning_steps = _build_template(html, matched, unreplayable)
    formats = [fmt.lower() for fmt in final_artifact.get("formats", ["html"])]
    if "html" not in formats:
        formats = ["html"] + formats

    return {
        "report_name": report_name,
        "anchor_date": anchor_date,
        "parameterized_sql": parameterized_sql,
        "metric_bindings": metric_bindings,
        "reasoning_steps": reasoning_steps,
        "rendering_spec": {
            "title": final_artifact.get("title", report_name),
            "template": template,
            "formats": formats,
        },
        "warnings": warnings,
        "unreplayable_sections": unreplayable,
    }
