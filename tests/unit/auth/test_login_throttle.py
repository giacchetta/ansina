from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ansina.auth.login_throttle import LoginThrottle, LoginThrottledError
from ansina.auth.models import LoginAttemptScope
from ansina.auth.repositories import LoginAttemptRepository
from ansina.config.settings import LoginSettings
from ansina.storage.database import Database

_START = datetime(2026, 1, 1, tzinfo=UTC)


class _Clock:
    """An injectable, manually-advanced clock — never real sleeping. Mirrors
    `test_sudo.py`'s own `_Clock` helper (copied, not imported — `tests/` has no
    `__init__.py`, so cross-file imports aren't reliable under this suite's
    `--import-mode=importlib`).
    """

    def __init__(self, start: datetime = _START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


_SETTINGS = LoginSettings(
    max_failed_attempts_per_username=3,
    max_failed_attempts_per_ip=5,
    attempt_window_seconds=300.0,
    lockout_seconds=900.0,
)


def _throttle(
    db: Database, *, settings: LoginSettings = _SETTINGS, clock: _Clock | None = None
) -> tuple[LoginThrottle, _Clock]:
    clock = clock or _Clock()
    return LoginThrottle(db, settings, clock=clock), clock


def test_clock_property_returns_the_injected_clock(db: Database) -> None:
    """`api.routes.login` (#50) mints the resulting token with this exact clock,
    rather than a second, independently-resolved `utc_now()` — the same seam
    `OidcLoginService.clock` already exposes.
    """
    throttle, clock = _throttle(db)

    assert throttle.clock is clock


def test_check_allows_a_fresh_username_and_ip(db: Database) -> None:
    throttle, _clock = _throttle(db)

    throttle.check("alice", "10.0.0.1")  # no exception


def test_max_failed_attempts_locks_the_username_for_lockout_seconds(
    db: Database,
) -> None:
    throttle, clock = _throttle(db)

    for _ in range(3):  # each failure a distinct IP, so the IP bucket never trips
        throttle.record_failure("alice", f"10.0.0.{_}")

    with pytest.raises(LoginThrottledError) as excinfo:
        throttle.check("alice", "10.0.0.99")
    assert excinfo.value.details["retry_after_seconds"] == pytest.approx(900.0)

    # The lock lapses on its own once lockout_seconds elapses — no sleeping.
    clock.advance(_SETTINGS.lockout_seconds + 1.0)
    throttle.check("alice", "10.0.0.99")  # no exception


def test_max_failed_attempts_locks_the_ip_for_lockout_seconds(db: Database) -> None:
    throttle, clock = _throttle(db)

    for _ in range(5):  # each failure a distinct username, so no username bucket trips
        throttle.record_failure(f"user-{_}", "10.0.0.1")

    with pytest.raises(LoginThrottledError):
        throttle.check("someone-else", "10.0.0.1")

    clock.advance(_SETTINGS.lockout_seconds + 1.0)
    throttle.check("someone-else", "10.0.0.1")  # no exception


def test_many_distinct_usernames_from_one_ip_trip_only_the_ip_bucket(
    db: Database,
) -> None:
    """The AC's own scenario: a spray across many usernames never reaches the
    username threshold (3) but does reach the IP threshold (5).
    """
    throttle, _clock = _throttle(db)

    for i in range(5):
        throttle.record_failure(f"victim-{i}", "10.0.0.1")

    # No single username bucket reached 3 failures.
    throttle.check("victim-0", "10.0.0.2")  # different IP — not locked
    # But the IP itself is locked out regardless of which (even unseen) username.
    with pytest.raises(LoginThrottledError):
        throttle.check("never-seen-before", "10.0.0.1")


def test_an_unknown_username_accrues_failures_identically_to_a_real_one(
    db: Database,
) -> None:
    """Nothing about the throttle distinguishes a real username from a fictional
    one — the recorded state after N failures is identical either way. This is what
    keeps the throttle from being a user-enumeration oracle.
    """
    throttle, _clock = _throttle(db)
    attempts = LoginAttemptRepository(db)

    throttle.record_failure("real-user", "10.0.0.1")
    throttle.record_failure("does-not-exist", "10.0.0.2")

    real = attempts.get(LoginAttemptScope.USERNAME, "real-user")
    fake = attempts.get(LoginAttemptScope.USERNAME, "does-not-exist")
    assert real is not None
    assert fake is not None
    assert real.failed_count == fake.failed_count == 1
    assert real.locked_until == fake.locked_until is None


def test_username_bucket_keys_on_the_casefolded_username(db: Database) -> None:
    throttle, _clock = _throttle(db)
    attempts = LoginAttemptRepository(db)

    throttle.record_failure("Alice", "10.0.0.1")

    assert attempts.get(LoginAttemptScope.USERNAME, "alice") is not None
    assert attempts.get(LoginAttemptScope.USERNAME, "Alice") is None

    throttle.check("ALICE", "10.0.0.9")  # not locked yet, but proves the lookup folds
    for _ in range(2):
        throttle.record_failure("aLiCe", "10.0.0.2")
    with pytest.raises(LoginThrottledError):
        throttle.check("ALICE", "10.0.0.9")


def test_record_success_clears_both_buckets(db: Database) -> None:
    throttle, _clock = _throttle(db)
    attempts = LoginAttemptRepository(db)

    throttle.record_failure("alice", "10.0.0.1")
    throttle.record_failure("alice", "10.0.0.1")

    throttle.record_success("alice", "10.0.0.1")

    assert attempts.get(LoginAttemptScope.USERNAME, "alice") is None
    assert attempts.get(LoginAttemptScope.IP, "10.0.0.1") is None
    throttle.check("alice", "10.0.0.1")  # no exception


def test_a_failure_outside_the_attempt_window_resets_the_streak(db: Database) -> None:
    throttle, clock = _throttle(db)

    throttle.record_failure("alice", "10.0.0.1")
    throttle.record_failure("alice", "10.0.0.2")

    clock.advance(_SETTINGS.attempt_window_seconds + 1.0)
    throttle.record_failure("alice", "10.0.0.3")  # streak reset to 1, not the 3rd

    throttle.check("alice", "10.0.0.9")  # still not locked out


def test_retry_after_reports_the_longer_of_the_two_locks(db: Database) -> None:
    """Both buckets locked, at different times — `retry_after_seconds` reports the
    longer remaining wait, never the shorter one.
    """
    throttle, clock = _throttle(db)

    for _ in range(3):
        throttle.record_failure("alice", "10.0.0.1")  # locks the username bucket

    clock.advance(100.0)  # the IP bucket's lock (below) now outlasts the username's

    for i in range(1, 5):
        throttle.record_failure(f"other-{i}", "10.0.0.1")  # locks the IP bucket

    with pytest.raises(LoginThrottledError) as excinfo:
        throttle.check("alice", "10.0.0.1")
    # IP bucket was locked 100s later, so it has ~100s longer left than the username
    # bucket's remaining time.
    assert excinfo.value.details["retry_after_seconds"] == pytest.approx(900.0)


def test_a_stale_but_still_locked_row_resets_on_the_next_failure(db: Database) -> None:
    """A row can still be locked (`locked_until` in the future) while its own
    `attempt_window_seconds` has separately elapsed — nothing guarantees
    `lockout_seconds >= attempt_window_seconds`, so `delete_expired`'s sweep (which
    requires both unlocked *and* stale) leaves it in place. Proves the in-place
    window-reset branch inside `_bump` handles that case directly: a further failure
    resets the streak to 1 rather than accumulating from before the window.
    """
    settings = LoginSettings(
        max_failed_attempts_per_username=2,
        max_failed_attempts_per_ip=100,
        attempt_window_seconds=100.0,
        lockout_seconds=10_000.0,
    )
    throttle, clock = _throttle(db, settings=settings)

    throttle.record_failure("alice", "10.0.0.1")
    throttle.record_failure("alice", "10.0.0.2")  # 2nd failure -> locks "alice"

    clock.advance(150.0)  # past attempt_window_seconds, still within lockout_seconds

    throttle.record_failure("alice", "10.0.0.3")  # streak resets to 1, not a 3rd

    attempts = LoginAttemptRepository(db)
    row = attempts.get(LoginAttemptScope.USERNAME, "alice")
    assert row is not None
    assert row.failed_count == 1


def test_delete_expired_removes_a_dead_row_but_keeps_live_ones(db: Database) -> None:
    attempts = LoginAttemptRepository(db)
    throttle, clock = _throttle(db)

    # A row that will fall outside its streak window and never locked — dead.
    throttle.record_failure("stale-user", "10.0.0.1")
    # A row that reaches lockout — still locked, must survive the sweep.
    for _ in range(3):
        throttle.record_failure("locked-user", "10.0.0.2")
    clock.advance(_SETTINGS.attempt_window_seconds + 1.0)
    # A row inside a live streak (not locked, not outside its window) — must survive.
    throttle.record_failure("fresh-user", "10.0.0.3")

    # Triggering the sweep again via any failure runs delete_expired with the
    # advanced clock.
    throttle.record_failure("another-user", "10.0.0.4")

    assert attempts.get(LoginAttemptScope.USERNAME, "stale-user") is None
    assert attempts.get(LoginAttemptScope.USERNAME, "locked-user") is not None
    assert attempts.get(LoginAttemptScope.USERNAME, "fresh-user") is not None
