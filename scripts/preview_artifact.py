"""Preview the HTML artifact you built *before* saving its definition.

Before `save_report_definition`, the client only has an HTML *body fragment* --
the base page shell (styling) is added at render time. This script wraps that
fragment in the same base template the runner uses, so you can open the report in
a browser and see the original artifact before it is saved and parity-checked.

    python scripts/preview_artifact.py path/to/fragment.html
    python scripts/preview_artifact.py fragment.html --title "My Report" --out reports/_preview.html

It only reads the base template, never the database, so it is safe to run while
the MCP server is up. Empty `data-reasoning` paragraphs show a muted placeholder:
their narrative is recomputed at replay and is not part of the parity-checked
data, so it is intentionally blank here.
"""

from __future__ import annotations

import argparse
import re
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.db import PROJECT_ROOT, TEMPLATES_DIR  # noqa: E402

# A copy of the base page styles plus a preview-only hint that renders empty
# reasoning placeholders visibly, so the preview does not look broken.
_PREVIEW_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #1a1a1a; }}
    h1, h2 {{ color: #123; }}
    table {{ border-collapse: collapse; margin: 1rem 0; }}
    th, td {{ border: 1px solid #ccc; padding: 4px 10px; text-align: left; }}
    th {{ background: #f3f5f8; }}
    .headline {{ font-size: 1.4rem; font-weight: 600; }}
    /* Preview-only: reasoning prose is generated at replay, so it is empty now. */
    p[data-reasoning]:empty::before {{
      content: "\\21bb  recomputed at replay time";
      color: #999; font-style: italic;
    }}
    .preview-banner {{
      background: #fff8e1; border: 1px solid #f0d999; border-radius: 6px;
      padding: 8px 12px; margin-bottom: 1.5rem; font-size: 0.9rem; color: #7a5b00;
    }}
  </style>
</head>
<body>
<div class="preview-banner">Preview of the artifact <strong>before</strong>
save_report_definition. Data tables and headline numbers are what the parity
gate locks; reasoning sentences are recomputed at replay.</div>
{body}
</body>
</html>
"""


def _title_from_fragment(fragment: str) -> str:
    match = re.search(r"<h1\b[^>]*>(.*?)</h1>", fragment, re.IGNORECASE | re.DOTALL)
    if match:
        return re.sub(r"<[^>]+>", "", match.group(1)).strip() or "Report preview"
    return "Report preview"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "fragment",
        help="Path to the HTML body fragment you built (or '-' to read stdin).",
    )
    parser.add_argument("--title", help="Browser tab title (default: the <h1> text).")
    parser.add_argument(
        "--out",
        default=str(PROJECT_ROOT / "reports" / "_preview.html"),
        help="Where to write the preview page (default: reports/_preview.html).",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Write the file but do not open it in a browser.",
    )
    args = parser.parse_args()

    if not (TEMPLATES_DIR / "report_base.html.j2").exists():
        parser.error(f"base template not found under {TEMPLATES_DIR}")

    fragment = (
        sys.stdin.read()
        if args.fragment == "-"
        else Path(args.fragment).read_text(encoding="utf-8")
    )
    title = args.title or _title_from_fragment(fragment)
    page = _PREVIEW_PAGE.format(title=title, body=fragment)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"wrote preview to {out}")
    if not args.no_open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
