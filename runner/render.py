"""Jinja2 rendering shared by the parity gate and the runner.

The definition stores a body template (the artifact HTML with each table's rows
replaced by a loop over its named result). Rendering runs that body template
against the named results, then wraps the output in the base page template.

This module also offers small helpers the client can use to build a parity-safe
artifact: tables that carry data-result attributes and headline spans that carry
data-value attributes.
"""

from __future__ import annotations

from html import escape

from jinja2 import Environment, FileSystemLoader, select_autoescape

from server.db import TEMPLATES_DIR

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "j2"]),
)


def render_definition(definition: dict, results_by_name: dict[str, dict]) -> str:
    """Render a definition's template with the given named results.

    ``results_by_name`` maps result_name to {"columns": [...], "rows": [{...}]}.
    """
    body_template = _env.from_string(definition["template"])
    body_html = body_template.render(**results_by_name)
    base = _env.get_template("report_base.html.j2")
    return base.render(
        title=definition.get("title") or definition.get("report_name", "Report"),
        body=body_html,
    )


def build_table_html(result_name: str, columns: list[str], rows: list[dict]) -> str:
    """Build a data-result table from a query result (client convenience)."""
    head = "".join(f"<th>{escape(str(c))}</th>" for c in columns)
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{escape(str(row[c]))}</td>" for c in columns)
        body_rows.append(f"<tr>{cells}</tr>")
    return (
        f'<table data-result="{escape(result_name)}">'
        f"<thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        f"</table>"
    )


def build_value_span(result_name: str, field: str, value: object) -> str:
    """Build a data-value headline span (client convenience)."""
    ref = f"{result_name}.{field}"
    return f'<span data-value="{escape(ref)}">{escape(str(value))}</span>'
