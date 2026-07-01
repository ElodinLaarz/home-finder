from datetime import datetime, timezone
from pathlib import Path

import pytest

from homefinder.config import load_config
from homefinder.util import next_departure

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_example_config_is_valid():
    cfg = load_config(REPO_ROOT / "config.yaml")
    assert cfg.commute.max_minutes > 0
    assert cfg.filters.property_types == ["Single Family"]
    assert cfg.scoring.rubric, "example rubric must not be empty"
    assert 0 <= cfg.scoring.instant_threshold <= 10


def test_invalid_departure_values_rejected():
    from homefinder.config import DepartureConfig

    with pytest.raises(ValueError):
        DepartureConfig(day_of_week="funday", timezone="America/New_York")
    with pytest.raises(ValueError):
        DepartureConfig(day_of_week="monday", time="25:00", timezone="America/New_York")
    with pytest.raises(Exception):
        DepartureConfig(day_of_week="monday", timezone="Not/AZone")


def test_next_departure_same_week():
    # Wednesday 2026-07-01 12:00 UTC = 08:00 New York
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    dep = next_departure("thursday", "08:00", "America/New_York", now)
    assert dep > now
    assert dep.astimezone(timezone.utc).isoformat() == "2026-07-02T12:00:00+00:00"


def test_next_departure_rolls_to_next_week_when_passed():
    # It's Wednesday 13:00 NY; a Wednesday 08:00 slot must jump a full week.
    now = datetime(2026, 7, 1, 17, 0, tzinfo=timezone.utc)
    dep = next_departure("wednesday", "08:00", "America/New_York", now)
    assert dep.astimezone(timezone.utc).isoformat() == "2026-07-08T12:00:00+00:00"


def test_next_departure_is_always_future():
    now = datetime(2026, 7, 1, 11, 59, tzinfo=timezone.utc)  # 07:59 NY
    dep = next_departure("wednesday", "08:00", "America/New_York", now)
    # only 1 minute away -> inside the 5-minute safety margin -> next week
    assert (dep - now).days >= 6
