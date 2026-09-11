"""Display-timezone resolution and conversion helpers.

Storage stays UTC everywhere; this module only affects what users see:
rendered timestamps and the calendar-day buckets of the dashboard trend.
The zone is resolved once at application startup from configuration and
injected into the services and view helpers that need it, keeping every
layer below the app factory deterministic in tests.
"""

from datetime import date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class InvalidTimezoneError(ValueError):
    """Raised when the configured display timezone cannot be resolved."""


def resolve_display_timezone(name: str | None) -> tzinfo:
    """Resolve a configured IANA name, or fall back to the server's zone.

    The fallback observes the machine's current offset (the common case for
    this locally hosted tool). Unknown or malformed names fail fast with a
    clear error so a misconfigured deployment never silently shows UTC.
    """
    if name is None or not name.strip():
        local = datetime.now().astimezone().tzinfo
        if local is None:  # pragma: no cover - astimezone always sets tzinfo
            raise InvalidTimezoneError(
                "无法确定服务器本地时区，请配置 MCQ_DISPLAY_TIMEZONE。"
            )
        return local
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidTimezoneError(
            f'无效的显示时区 "{name}"；请使用 IANA 名称（例如 Asia/Shanghai）。'
        ) from exc


def to_display(moment: datetime, zone: tzinfo) -> datetime:
    """Convert an aware datetime into the display timezone."""
    return moment.astimezone(zone)


def display_day(moment: datetime, zone: tzinfo) -> date:
    """Return the calendar day of ``moment`` in the display timezone."""
    return moment.astimezone(zone).date()


def timezone_label(zone: tzinfo, *, now: datetime | None = None) -> str:
    """Render a compact ``UTC±HH:MM`` label for the zone's current offset."""
    moment = now or datetime.now(zone)
    offset = moment.astimezone(zone).utcoffset() or timedelta(0)
    if offset == timedelta(0):
        return "UTC"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"
