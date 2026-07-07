"""Minimal .env loader (no third-party dependency).

Loads KEY=VALUE pairs from a .env file at the project root into os.environ,
without overriding variables already present in the real environment. This makes
the optional LLM reasoning engine easy to configure locally: put ANTHROPIC_API_KEY
(and optionally POC_REASONING) in a .env file instead of exporting them.

The .env file is gitignored. Never commit real secrets; commit .env.example.
"""

from __future__ import annotations

import os
from pathlib import Path

from server.db import PROJECT_ROOT


def load_dotenv(path: Path | None = None) -> None:
    """Populate os.environ from a .env file, leaving existing vars untouched."""
    path = path or (PROJECT_ROOT / ".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)
