import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scanner.config import Config  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
# All fixtures describe a December 2026 run; pin "today" so plausibility
# windows and year-less date headings stay deterministic forever.
TODAY = date(2026, 8, 8)


@pytest.fixture
def cfg():
    return Config(jitter_seconds=0, respect_robots=False)


@pytest.fixture
def today():
    return TODAY


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")
