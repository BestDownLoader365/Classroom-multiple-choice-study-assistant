"""Fixed-interval spaced-repetition (SRS) scheduling rules.

A corrected wrong question carries an ``srs_level`` and a ``next_review_at``
timestamp on its wrong-question record. Answering a due review correctly
advances the level (lengthening the interval); any wrong answer sends the
question back through the regular correction flow first, and completing that
correction restarts the schedule at level 0.

The helpers are pure and accept ``now`` explicitly so scheduling stays
deterministic and easy to test. All timestamps are aware UTC datetimes and
ISO strings in storage.
"""

from datetime import datetime, timedelta, timezone

# Days until the next review for each SRS level. The final entry caps every
# level at or beyond it, so well-retained questions repeat monthly at most.
SRS_INTERVAL_DAYS: tuple[int, ...] = (1, 3, 7, 15, 30)

# A freshly corrected question always restarts at this level (1 day).
INITIAL_SRS_LEVEL = 0


def utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


def parse_timestamp(value: str) -> datetime:
    """Parse a stored ISO timestamp; naive values are interpreted as UTC."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def interval_days_for_level(level: int) -> int:
    """Return the review interval in days for ``level``, capped at the top."""
    clamped = min(max(int(level), 0), len(SRS_INTERVAL_DAYS) - 1)
    return SRS_INTERVAL_DAYS[clamped]


def next_review_at_for_level(level: int, now: datetime) -> str:
    """Return the ISO timestamp one interval of ``level`` after ``now``."""
    return (now + timedelta(days=interval_days_for_level(level))).isoformat()


def initial_schedule(now: datetime) -> tuple[int, str]:
    """Return the level-0 schedule applied when a correction completes."""
    return INITIAL_SRS_LEVEL, next_review_at_for_level(INITIAL_SRS_LEVEL, now)


def advanced_schedule(level: int, now: datetime) -> tuple[int, str]:
    """Return the schedule after a due review is answered correctly."""
    new_level = max(int(level), 0) + 1
    return new_level, next_review_at_for_level(new_level, now)


def is_due(next_review_at: str | None, now: datetime) -> bool:
    """Return whether a scheduled review is due at ``now`` (inclusive)."""
    if not next_review_at:
        return False
    return parse_timestamp(next_review_at) <= now
