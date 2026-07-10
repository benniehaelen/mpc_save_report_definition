"""Pytest configuration: ensure the project root is importable and the database
is seeded once per test session.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session", autouse=True)
def _deterministic_reasoning():
    """Keep the suite offline and deterministic.

    `server/reasoning.py` loads a gitignored .env, so a developer who has opted
    into `POC_REASONING=anthropic` would otherwise have every test that saves or
    replays a report call the Anthropic API -- slow, billed, and non-deterministic.
    Tests that exercise the LLM engine set the variable themselves via monkeypatch.
    """
    import os

    previous = os.environ.get("POC_REASONING")
    os.environ["POC_REASONING"] = "heuristic"
    yield
    if previous is None:
        os.environ.pop("POC_REASONING", None)
    else:
        os.environ["POC_REASONING"] = previous


@pytest.fixture(scope="session", autouse=True)
def _seeded_database():
    """Seed a fresh poc.duckdb before the test session runs."""
    from data import seed

    seed.main()
    yield
