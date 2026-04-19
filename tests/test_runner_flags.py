"""Runner flags: as_of vs today validation."""
from __future__ import annotations

from datetime import date

import pytest

from core.runner import validate_run_dates


def test_validate_run_dates_rejects_as_of_after_today():
    with pytest.raises(ValueError, match="as_of"):
        validate_run_dates(today=date(2026, 4, 17), as_of=date(2026, 4, 20))


def test_validate_run_dates_allows_none_as_of():
    validate_run_dates(today=date(2026, 4, 18), as_of=None)


def test_validate_run_dates_allows_as_of_equal_today():
    validate_run_dates(today=date(2026, 4, 17), as_of=date(2026, 4, 17))
