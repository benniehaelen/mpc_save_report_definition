"""Lint a submitted v2 artifact.

Two classes of defect, neither of which the parity gate would catch:

* **Author-written template logic.** The compiler *produces* Jinja; an artifact
  that already contains ``{{ }}`` or ``{% %}`` would have it rendered at replay,
  which is a code path nobody reviewed. Reject it.
* **Literal period labels.** A heading that reads ``Gap to #1 (Q1'25)`` is frozen
  prose. It looks right the day it is written and lies at every later replay,
  and because parity only compares data values, nothing else notices. The fix is
  to bind the label to a result, so this only warns -- it does not block a save.

Findings are strings, shaped like the compiler's existing `unreplayable_sections`
entries.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

_JINJA_DELIMITER = re.compile(r"{{|}}|{%|%}")

# Q1'25, Q4'24 -- the shape a hand-typed quarter label takes.
_QUARTER_LITERAL = re.compile(r"\bQ[1-4]'\d{2}\b")

# Text inside any of these is legitimately allowed to name a period: a data-value
# is bound to a result, and editorial prose is authored against a known date and
# replayed verbatim (with a staleness banner when its watch fires).
_BOUND_ATTRS = (
    "data-value",
    "data-editorial",
    "data-reasoning",
    "data-chart",
    "data-result",
)

_LABEL_SELECTOR = "h1, h2, h3, h4, h5, h6, .kpi-label, [data-kpi-label]"


def _inside_bound_element(node) -> bool:
    for ancestor in node.parents:
        attrs = getattr(ancestor, "attrs", None)
        if attrs and any(attr in attrs for attr in _BOUND_ATTRS):
            return True
    return False


def lint(html: str) -> tuple[list[str], list[str]]:
    """Return (unreplayable_sections, warnings) for a submitted artifact."""
    unreplayable: list[str] = []
    warnings: list[str] = []

    if _JINJA_DELIMITER.search(html):
        unreplayable.append(
            "artifact contains template delimiters ({{ }} or {% %}); the compiler "
            "writes the template, the client supplies data"
        )

    soup = BeautifulSoup(html, "html.parser")
    for label in soup.select(_LABEL_SELECTOR):
        if _inside_bound_element(label):
            continue
        for text in label.find_all(string=True):
            # A data-value span nested in a heading is the correct way to show a
            # quarter, so skip the text nodes that live inside one.
            if _inside_bound_element(text):
                continue
            match = _QUARTER_LITERAL.search(str(text))
            if match:
                warnings.append(
                    f"literal period label {match.group(0)!r} in <{label.name}>; "
                    "bind it to a result with data-value so it moves with the "
                    "report date"
                )
    return unreplayable, warnings
