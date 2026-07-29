"""Shared pytest configuration.

Makes ``tests/`` importable so suites can share helpers (``fixtures.corpus``)
without each test file manipulating ``sys.path`` itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent

if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))
