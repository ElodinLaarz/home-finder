"""Small shared helpers with no project dependencies."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def next_departure(
    day_of_week: str,
    hhmm: str,
    tz_name: str,
    now: datetime | None = None,
) -> datetime:
    """Next future occurrence of weekday+time in the given zone, as aware UTC.

    Google Routes rejects departure times in the past, so if the slot is now or
    has just passed (5-minute margin), roll to next week.
    """
    tz = ZoneInfo(tz_name)
    now = now or datetime.now(timezone.utc)
    now_local = now.astimezone(tz)
    hour, minute = (int(p) for p in hhmm.split(":"))
    days_ahead = (WEEKDAYS[day_of_week.lower()] - now_local.weekday()) % 7
    candidate = (now_local + timedelta(days=days_ahead)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if candidate <= now_local + timedelta(minutes=5):
        candidate += timedelta(days=7)
    return candidate.astimezone(timezone.utc)
