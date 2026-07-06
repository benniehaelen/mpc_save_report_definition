"""Rendering shared by the parity gate and the runner.

The definition's rendering_spec holds a body template (the artifact HTML with
each table's rows replaced by a loop, data-value numbers replaced by expressions,
and data-reasoning elements replaced by narrative expressions). Rendering runs
that body template against the named results and the reasoning narratives, then
wraps the output in the base page template. The same spec also produces Markdown,
so both formats come from one rendering spec.

Convenience builders let the client construct a parity-safe artifact: tables with
data-result attributes, headline spans with data-value attributes, and reasoning
placeholders with data-reasoning attributes.
"""

from __future__ import annotations

import re
from html import escape

from jinja2 import Environment, FileSystemLoader, select_autoescape

from server.db import TEMPLATES_DIR

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "j2"]),
)

_TAG_RE = re.compile(r"<[^>]+>")
_TABLE_RE = re.compile(
    r'<table\b[^>]*\bdata-result="([^"]+)"[^>]*>(.*?)</table>',
    re.IGNORECASE | re.DOTALL,
)
_THEAD_RE = re.compile(r"<thead\b[^>]*>(.*?)</thead>", re.IGNORECASE | re.DOTALL)
_TBODY_RE = re.compile(r"<tbody\b[^>]*>(.*?)</tbody>", re.IGNORECASE | re.DOTALL)
_TH_RE = re.compile(r"<th\b[^>]*>(.*?)</th>", re.IGNORECASE | re.DOTALL)
_TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_HEADING_RE = re.compile(r"<h([1-3])\b[^>]*>(.*?)</h\1>", re.IGNORECASE | re.DOTALL)
_PARA_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)


def _render_body(definition: dict, results_by_name: dict, reasoning: dict) -> str:
    template = _env.from_string(definition["rendering_spec"]["template"])
    return template.render(reasoning=reasoning or {}, **results_by_name)


def render_html(
    definition: dict,
    results_by_name: dict[str, dict],
    reasoning: dict[str, str] | None = None,
) -> str:
    """Render the definition to a full HTML page."""
    body_html = _render_body(definition, results_by_name, reasoning or {})
    base = _env.get_template("report_base.html.j2")
    return base.render(
        title=definition["rendering_spec"].get("title")
        or definition.get("report_name", "Report"),
        body=body_html,
    )


def render_markdown(
    definition: dict,
    results_by_name: dict[str, dict],
    reasoning: dict[str, str] | None = None,
) -> str:
    """Render the definition to Markdown from the same rendering spec."""
    body_html = _render_body(definition, results_by_name, reasoning or {})
    md_body = _html_to_markdown(body_html)
    if md_body.lstrip().startswith("# "):
        # The body already carries its own top-level heading.
        return md_body
    title = definition["rendering_spec"].get("title") or definition.get(
        "report_name", "Report"
    )
    return f"# {title}\n\n{md_body}"


def render(
    definition: dict,
    results_by_name: dict[str, dict],
    reasoning: dict[str, str] | None = None,
    fmt: str = "html",
) -> str:
    """Render to the requested format ('html' or 'md')."""
    if fmt == "md":
        return render_markdown(definition, results_by_name, reasoning)
    if fmt == "html":
        return render_html(definition, results_by_name, reasoning)
    raise ValueError(f"Unsupported render format: {fmt}")


def _text(html: str) -> str:
    return _TAG_RE.sub("", html).strip()


def _table_to_markdown(inner: str) -> str:
    thead = _THEAD_RE.search(inner)
    headers = [_text(th) for th in _TH_RE.findall(thead.group(1))] if thead else []
    body = _TBODY_RE.search(inner)
    rows_html = body.group(1) if body else inner
    rows = []
    for tr in _TR_RE.findall(rows_html):
        cells = [_text(td) for td in _TD_RE.findall(tr)]
        if cells:
            rows.append(cells)
    if not headers and rows:
        headers = [f"col{i + 1}" for i in range(len(rows[0]))]
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for cells in rows:
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _html_to_markdown(html: str) -> str:
    """Convert the controlled artifact HTML subset to Markdown."""
    blocks: list[str] = []
    pos = 0
    # Walk headings, paragraphs, and tables in document order.
    pattern = re.compile(
        r"<h([1-3])\b[^>]*>(.*?)</h\1>"
        r"|<p\b[^>]*>(.*?)</p>"
        r"|<table\b[^>]*\bdata-result=\"[^\"]+\"[^>]*>(.*?)</table>",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(html):
        _ = pos
        if match.group(1):  # heading
            level = int(match.group(1))
            blocks.append("#" * level + " " + _text(match.group(2)))
        elif match.group(3) is not None:  # paragraph
            text = _text(match.group(3))
            if text:
                blocks.append(text)
        else:  # table
            blocks.append(_table_to_markdown(match.group(4)))
    return "\n\n".join(blocks) + "\n"


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


def build_reasoning_para(
    step_id: str, result_name: str, field: str, agg: str = "max"
) -> str:
    """Build a data-reasoning placeholder the runner fills with narrative."""
    over = f"{result_name}.{field}"
    return (
        f'<p data-reasoning="{escape(step_id)}" data-over="{escape(over)}" '
        f'data-agg="{escape(agg)}"></p>'
    )
