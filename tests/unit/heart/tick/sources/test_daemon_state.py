from __future__ import annotations

from collections.abc import Callable, Mapping

from ansina.heart.tick.decision import TickDecision
from ansina.heart.tick.sources.daemon_state import DaemonStateSource


class _FakeReadiness:
    def __init__(self, checks: Mapping[str, bool]) -> None:
        self._checks = dict(checks)

    def snapshot(self) -> Mapping[str, bool]:
        return dict(self._checks)


class _FakeDatabase:
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy

    def is_healthy(self) -> bool:
        return self.healthy


class _FakeTickStats:
    def __init__(
        self,
        *,
        ticks_run: int = 0,
        last_decision: TickDecision | None = None,
        last_duration_seconds: float | None = None,
        failures_total: int = 0,
        consecutive_failures: int = 0,
        consecutive_overruns: int = 0,
    ) -> None:
        self.ticks_run = ticks_run
        self.last_decision = last_decision
        self.last_duration_seconds = last_duration_seconds
        self.failures_total = failures_total
        self.consecutive_failures = consecutive_failures
        self.consecutive_overruns = consecutive_overruns


def _clock(values: list[float]) -> Callable[[], float]:
    it = iter(values)
    return lambda: next(it)


# --- liveness: the AC's own test -----------------------------------------------------


def test_collect_reflects_live_state_across_two_calls() -> None:
    """Mutate readiness + database health *between* two `collect()` calls and assert
    the output changed — proves this source reads live state, never a cached snapshot.
    """
    readiness = _FakeReadiness({"database": True, "heart": True})
    database = _FakeDatabase(healthy=True)
    source = DaemonStateSource(
        _FakeTickStats(), database=database, readiness=readiness, clock=lambda: 0.0
    )

    first = source.collect()
    assert all("unhealthy" not in item.text for item in first)
    assert all("failing" not in item.text for item in first)

    database.healthy = False
    readiness._checks["heart"] = False

    second = source.collect()
    assert any("currently unhealthy" in item.text for item in second)
    assert any("currently failing: heart" in item.text for item in second)
    assert first != second


# --- shape: always exactly five items, named "daemon_state" -------------------------


def test_collect_returns_exactly_five_items_named_daemon_state() -> None:
    source = DaemonStateSource(
        _FakeTickStats(),
        database=_FakeDatabase(),
        readiness=_FakeReadiness({"database": True}),
    )

    items = source.collect()

    assert len(items) == 5
    assert all(item.source == "daemon_state" for item in items)
    assert source.name == "daemon_state"


# --- uptime -----------------------------------------------------------------------


def test_uptime_is_measured_from_construction_time() -> None:
    clock = _clock([0.0, 125.0])  # constructed at t=0, collected at t=125 (2m05s)
    source = DaemonStateSource(
        _FakeTickStats(),
        database=_FakeDatabase(),
        readiness=_FakeReadiness({}),
        clock=clock,
    )

    items = source.collect()

    assert items[0].text == "Daemon uptime: 2m."


def test_uptime_formats_hours_and_minutes() -> None:
    clock = _clock([0.0, 7384.0])  # 2h03m04s -> "2h03m"
    source = DaemonStateSource(
        _FakeTickStats(),
        database=_FakeDatabase(),
        readiness=_FakeReadiness({}),
        clock=clock,
    )

    items = source.collect()

    assert items[0].text == "Daemon uptime: 2h03m."


def test_uptime_formats_seconds_only_under_a_minute() -> None:
    clock = _clock([0.0, 42.0])
    source = DaemonStateSource(
        _FakeTickStats(),
        database=_FakeDatabase(),
        readiness=_FakeReadiness({}),
        clock=clock,
    )

    items = source.collect()

    assert items[0].text == "Daemon uptime: 42s."


# --- readiness ----------------------------------------------------------------------


def test_readiness_item_reports_all_passing_with_the_nominal_priority() -> None:
    source = DaemonStateSource(
        _FakeTickStats(),
        database=_FakeDatabase(),
        readiness=_FakeReadiness({"database": True, "heart": True, "startup": True}),
    )

    item = source.collect()[1]

    assert item.text == "All 3 readiness check(s) currently passing."
    assert item.priority == 10


def test_readiness_item_names_failing_checks_sorted_at_fault_priority() -> None:
    source = DaemonStateSource(
        _FakeTickStats(),
        database=_FakeDatabase(),
        readiness=_FakeReadiness({"startup": False, "database": True, "heart": False}),
    )

    item = source.collect()[1]

    assert item.text == "Readiness check(s) currently failing: heart, startup."
    assert item.priority == 100


# --- database -------------------------------------------------------------------


def test_database_item_healthy_is_nominal_priority() -> None:
    source = DaemonStateSource(
        _FakeTickStats(),
        database=_FakeDatabase(healthy=True),
        readiness=_FakeReadiness({}),
    )

    item = source.collect()[2]

    assert item.text == "Database connection is healthy."
    assert item.priority == 10


def test_database_item_unhealthy_is_fault_priority() -> None:
    source = DaemonStateSource(
        _FakeTickStats(),
        database=_FakeDatabase(healthy=False),
        readiness=_FakeReadiness({}),
    )

    item = source.collect()[2]

    assert item.text == "Database connection is currently unhealthy."
    assert item.priority == 100


# --- tick activity --------------------------------------------------------------


def test_activity_item_before_any_tick_has_run() -> None:
    source = DaemonStateSource(
        _FakeTickStats(ticks_run=0, last_decision=None),
        database=_FakeDatabase(),
        readiness=_FakeReadiness({}),
    )

    item = source.collect()[3]

    assert item.text == "Ticks completed: 0. No tick has run yet."
    assert item.priority == 0


def test_activity_item_after_a_completed_tick() -> None:
    source = DaemonStateSource(
        _FakeTickStats(
            ticks_run=7, last_decision=TickDecision.IDLE, last_duration_seconds=0.42
        ),
        database=_FakeDatabase(),
        readiness=_FakeReadiness({}),
    )

    item = source.collect()[3]

    assert item.text == "Ticks completed: 7. Last decision: idle (took 0.42s)."


def test_activity_item_handles_a_missing_duration() -> None:
    """`last_duration_seconds` can only be `None` alongside `last_decision is None` in
    `TickLoop` itself, but this source treats the two independently rather than assuming
    that invariant holds forever.
    """
    source = DaemonStateSource(
        _FakeTickStats(
            ticks_run=1, last_decision=TickDecision.ACT, last_duration_seconds=None
        ),
        database=_FakeDatabase(),
        readiness=_FakeReadiness({}),
    )

    item = source.collect()[3]

    assert item.text == "Ticks completed: 1. Last decision: act (took unknown)."


# --- tick faults ------------------------------------------------------------------


def test_faults_item_nominal_when_nothing_has_ever_failed() -> None:
    source = DaemonStateSource(
        _FakeTickStats(), database=_FakeDatabase(), readiness=_FakeReadiness({})
    )

    item = source.collect()[4]

    assert item.text == "No tick failures or overruns recorded."
    assert item.priority == 10


def test_faults_item_recovered_stays_nominal_priority_but_names_the_history() -> None:
    """Lifetime `failures_total` never resets, so a fully recovered loop (consecutive
    counters both zero) must not be pinned at fault priority forever just because it
    failed once, long ago.
    """
    source = DaemonStateSource(
        _FakeTickStats(
            failures_total=3, consecutive_failures=0, consecutive_overruns=0
        ),
        database=_FakeDatabase(),
        readiness=_FakeReadiness({}),
    )

    item = source.collect()[4]

    assert (
        item.text == "No current tick failures or overruns (3 historical failure(s))."
    )
    assert item.priority == 10


def test_faults_item_reports_live_consecutive_failures_at_fault_priority() -> None:
    source = DaemonStateSource(
        _FakeTickStats(
            failures_total=5, consecutive_failures=2, consecutive_overruns=0
        ),
        database=_FakeDatabase(),
        readiness=_FakeReadiness({}),
    )

    item = source.collect()[4]

    assert item.text == (
        "Tick failures: 2 consecutive, 5 total. Consecutive slow (overrun) ticks: 0."
    )
    assert item.priority == 100


def test_faults_item_reports_live_consecutive_overruns_at_fault_priority() -> None:
    source = DaemonStateSource(
        _FakeTickStats(
            failures_total=0, consecutive_failures=0, consecutive_overruns=3
        ),
        database=_FakeDatabase(),
        readiness=_FakeReadiness({}),
    )

    item = source.collect()[4]

    assert item.text == (
        "Tick failures: 0 consecutive, 0 total. Consecutive slow (overrun) ticks: 3."
    )
    assert item.priority == 100
