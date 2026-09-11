from __future__ import annotations

from datetime import UTC, datetime

from ansina.auth.clock import iso, parse_iso, utc_now


def test_utc_now_returns_a_timezone_aware_utc_datetime() -> None:
    now = utc_now()

    assert now.tzinfo is UTC


def test_iso_is_millisecond_precision_and_ends_in_z() -> None:
    dt = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)

    formatted = iso(dt)

    assert formatted == "2026-01-02T03:04:05.123Z"


def test_iso_values_sort_identically_as_text_and_as_datetimes() -> None:
    """`SudoGrantRepository.find_active`'s `expires_at > ?` comparison, and issue
    #28's `last_used_at` staleness check, both compare two `iso()` values as plain
    strings rather than parsing them — this pins the property that makes that safe.
    """
    earlier = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    later = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)

    assert (iso(earlier) < iso(later)) == (earlier < later)


def test_parse_iso_round_trips_with_iso() -> None:
    dt = datetime(2026, 6, 15, 12, 30, 45, 678000, tzinfo=UTC)

    assert parse_iso(iso(dt)) == dt
