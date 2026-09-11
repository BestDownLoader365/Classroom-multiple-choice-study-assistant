"""Tests for display-timezone resolution and timestamp presentation."""

from datetime import datetime, timedelta, timezone, tzinfo

import pytest
from zoneinfo import ZoneInfo

from app.services import (
    InvalidTimezoneError,
    display_day,
    resolve_display_timezone,
    timezone_label,
    to_display,
)
from app.web import view_helpers
from tests.test_web import make_app

UTC = timezone.utc
PLUS_8 = timezone(timedelta(hours=8))
MINUS_5 = timezone(timedelta(hours=-5))
PLUS_5_30 = timezone(timedelta(hours=5, minutes=30))
MOMENT = datetime(2026, 3, 1, 16, 30, tzinfo=UTC)


def test_resolve_falls_back_to_server_local_zone():
    zone = resolve_display_timezone(None)
    assert isinstance(zone, tzinfo)
    local_now = datetime.now(zone)
    assert local_now.utcoffset() == datetime.now().astimezone().utcoffset()
    # Blank configuration behaves the same as an unset one.
    assert resolve_display_timezone("  ") is not None


def test_resolve_accepts_iana_names_and_rejects_invalid_ones():
    assert resolve_display_timezone("Asia/Shanghai") == ZoneInfo("Asia/Shanghai")
    assert resolve_display_timezone("UTC") == ZoneInfo("UTC")
    with pytest.raises(InvalidTimezoneError, match="无效的显示时区"):
        resolve_display_timezone("Mars/Olympus")


def test_to_display_and_display_day_convert_calendar_day():
    converted = to_display(MOMENT, PLUS_8)

    assert converted.hour == 0 and converted.minute == 30
    assert display_day(MOMENT, PLUS_8).isoformat() == "2026-03-02"
    assert display_day(MOMENT, UTC).isoformat() == "2026-03-01"
    # A real IANA zone (with tzdata) converts identically to the fixed offset.
    assert to_display(MOMENT, ZoneInfo("Asia/Shanghai")).utcoffset() == timedelta(
        hours=8
    )


def test_timezone_label_formats_offsets():
    assert timezone_label(UTC) == "UTC"
    assert timezone_label(PLUS_8) == "UTC+08:00"
    assert timezone_label(MINUS_5) == "UTC-05:00"
    assert timezone_label(PLUS_5_30) == "UTC+05:30"


def test_format_datetime_renders_in_display_zone():
    assert (
        view_helpers.format_datetime(MOMENT.isoformat(), zone=PLUS_8)
        == "2026-03-02 00:30"
    )
    assert (
        view_helpers.format_datetime(MOMENT.isoformat(), zone=UTC)
        == "2026-03-01 16:30"
    )
    assert view_helpers.format_datetime(None, zone=PLUS_8) == "—"


def test_invalid_display_timezone_fails_fast_at_startup(tmp_path, valid_payload):
    with pytest.raises(InvalidTimezoneError):
        make_app(tmp_path, valid_payload, DISPLAY_TIMEZONE="Mars/Olympus")
