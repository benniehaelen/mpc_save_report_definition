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
def _seeded_database():
    """Seed a fresh poc.duckdb before the test session runs."""
    from data import seed

    seed.main()
    yield
