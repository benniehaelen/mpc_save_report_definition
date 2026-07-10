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

    `server/reasoning.py` and `server/extractor.py` both load a gitignored .env, so
    a developer who has opted into `POC_REASONING=anthropic` or
    `POC_DISTILLER=anthropic` would otherwise have every test that saves or replays
    a report call the Anthropic API -- slow, billed, and non-deterministic.
    Tests that exercise the LLM engines set the variables themselves via monkeypatch.
    """
    import os

    pinned = {"POC_REASONING": "heuristic", "POC_DISTILLER": "deterministic"}
    previous = {name: os.environ.get(name) for name in pinned}
    os.environ.update(pinned)
    yield
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture(scope="session", autouse=True)
def _seeded_database():
    """Seed a fresh poc.duckdb before the test session runs."""
    from data import seed

    seed.main()
    yield
