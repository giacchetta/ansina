from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ansina.auth.clock import iso
from ansina.heart.tick.snapshot import build_prompt
from ansina.heart.tick.sources.opencode_state import OpencodeStateSource


@dataclass(frozen=True, slots=True)
class _FakeRunningRun:
    id: str
    started_at: str | None
    last_event_at: str | None


@dataclass(frozen=True, slots=True)
class _FakeOutcomeCounts:
    failed: int = 0
    timeout: int = 0
    permission_denied: int = 0


class _FakeProbe:
    def __init__(
        self,
        *,
        intake_paused: bool = False,
        counts: Mapping[str, int] | None = None,
        running: Sequence[_FakeRunningRun] = (),
        outcomes: _FakeOutcomeCounts | None = None,
    ) -> None:
        self.intake_paused = intake_paused
        self._counts = dict(counts or {})
        self._running = list(running)
        self._outcomes = outcomes if outcomes is not None else _FakeOutcomeCounts()
        self.recent_outcome_calls: list[str] = []

    def counts_by_status(self) -> Mapping[str, int]:
        return dict(self._counts)

    def running_runs(self) -> Sequence[_FakeRunningRun]:
        return list(self._running)

    def recent_outcome_counts(self, *, since: str) -> _FakeOutcomeCounts:
        self.recent_outcome_calls.append(since)
        return self._outcomes


def _clock(value: datetime) -> Callable[[], datetime]:
    return lambda: value


_NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)


# --- shape: always exactly three items, named "opencode_state" ----------------------


def test_collect_returns_exactly_three_items_named_opencode_state() -> None:
    source = OpencodeStateSource(
        _FakeProbe(), stall_seconds=300, recent_window_minutes=15, clock=_clock(_NOW)
    )

    items = source.collect()

    assert len(items) == 3
    assert all(item.source == "opencode_state" for item in items)
    assert source.name == "opencode_state"


# --- activity item --------------------------------------------------------------


def test_activity_item_reports_counts_and_accepting_intake() -> None:
    source = OpencodeStateSource(
        _FakeProbe(counts={"queued": 2, "running": 1}, intake_paused=False),
        stall_seconds=300,
        recent_window_minutes=15,
        clock=_clock(_NOW),
    )

    item = source.collect()[0]

    assert item.text == "OpenCode: 2 queued, 1 running. Intake accepting new runs."
    assert item.priority == 5


def test_activity_item_reports_paused_intake() -> None:
    source = OpencodeStateSource(
        _FakeProbe(intake_paused=True), stall_seconds=300, recent_window_minutes=15
    )

    item = source.collect()[0]

    assert item.text == "OpenCode: 0 queued, 0 running. Intake paused."


def test_activity_item_defaults_missing_statuses_to_zero() -> None:
    """`counts_by_status()` omits a status with zero current rows rather than
    reporting an explicit `0` — this source must still render `0` for it."""
    source = OpencodeStateSource(
        _FakeProbe(counts={}), stall_seconds=300, recent_window_minutes=15
    )

    item = source.collect()[0]

    assert "0 queued, 0 running" in item.text


# --- stalled-run detection --------------------------------------------------------


def test_stalled_item_reports_no_stalled_runs_when_none_are_running() -> None:
    source = OpencodeStateSource(
        _FakeProbe(running=()),
        stall_seconds=300,
        recent_window_minutes=15,
        clock=_clock(_NOW),
    )

    item = source.collect()[1]

    assert item.text == "No stalled OpenCode runs."
    assert item.priority == 5


def test_stalled_item_flags_a_run_with_no_recent_event_past_the_threshold() -> None:
    """A run started long ago with no event at all — `started_at` is the staleness
    reference when `last_event_at` is `None`.
    """
    old_start = iso(_NOW - timedelta(seconds=400))
    source = OpencodeStateSource(
        _FakeProbe(running=[_FakeRunningRun("run-1", old_start, None)]),
        stall_seconds=300,
        recent_window_minutes=15,
        clock=_clock(_NOW),
    )

    item = source.collect()[1]

    assert "1 OpenCode run(s) stalled" in item.text
    assert "run-1" in item.text
    assert item.priority == 50


def test_stalled_item_distinguishes_long_but_healthy_from_stalled() -> None:
    """The AC's own required distinction: a run with an old `started_at` but a
    *recent* `last_event_at` is still actively producing events and must not be
    reported as stalled — only a run whose most recent signal (of either kind) is
    older than `stall_seconds` counts.
    """
    very_old_start = iso(_NOW - timedelta(hours=5))
    recent_event = iso(_NOW - timedelta(seconds=10))
    old_event = iso(_NOW - timedelta(seconds=400))
    source = OpencodeStateSource(
        _FakeProbe(
            running=[
                _FakeRunningRun("healthy-long-runner", very_old_start, recent_event),
                _FakeRunningRun("truly-stalled", very_old_start, old_event),
            ]
        ),
        stall_seconds=300,
        recent_window_minutes=15,
        clock=_clock(_NOW),
    )

    item = source.collect()[1]

    assert "healthy-long-runner" not in item.text
    assert "truly-stalled" in item.text
    assert "1 OpenCode run(s) stalled" in item.text


def test_stalled_item_skips_a_run_with_neither_last_event_nor_started_at() -> None:
    """Defensive: can't happen through any designed path, but must not crash the
    whole snapshot collection either.
    """
    source = OpencodeStateSource(
        _FakeProbe(running=[_FakeRunningRun("malformed", None, None)]),
        stall_seconds=300,
        recent_window_minutes=15,
        clock=_clock(_NOW),
    )

    item = source.collect()[1]

    assert item.text == "No stalled OpenCode runs."


def test_stalled_item_reports_multiple_stalled_runs() -> None:
    old_event = iso(_NOW - timedelta(seconds=301))
    source = OpencodeStateSource(
        _FakeProbe(
            running=[
                _FakeRunningRun("a", None, old_event),
                _FakeRunningRun("b", None, old_event),
            ]
        ),
        stall_seconds=300,
        recent_window_minutes=15,
        clock=_clock(_NOW),
    )

    item = source.collect()[1]

    assert "2 OpenCode run(s) stalled" in item.text
    assert "a" in item.text and "b" in item.text


# --- recent outcomes ---------------------------------------------------------------


def test_recent_outcomes_item_nominal_when_nothing_has_failed() -> None:
    source = OpencodeStateSource(
        _FakeProbe(outcomes=_FakeOutcomeCounts()),
        stall_seconds=300,
        recent_window_minutes=15,
        clock=_clock(_NOW),
    )

    item = source.collect()[2]

    assert item.text == (
        "No OpenCode failures, timeouts, or permission denials in the last 15m."
    )
    assert item.priority == 5


def test_recent_outcomes_item_reports_nonzero_counts_at_fault_priority() -> None:
    source = OpencodeStateSource(
        _FakeProbe(
            outcomes=_FakeOutcomeCounts(failed=2, timeout=1, permission_denied=3)
        ),
        stall_seconds=300,
        recent_window_minutes=15,
        clock=_clock(_NOW),
    )

    item = source.collect()[2]

    assert item.text == (
        "OpenCode, last 15m: 2 failed, 1 timed out, 3 permission denial(s)."
    )
    assert item.priority == 50


def test_recent_outcomes_item_queries_since_the_configured_window() -> None:
    probe = _FakeProbe(outcomes=_FakeOutcomeCounts())
    source = OpencodeStateSource(
        probe, stall_seconds=300, recent_window_minutes=20, clock=_clock(_NOW)
    )

    source.collect()

    assert probe.recent_outcome_calls == [iso(_NOW - timedelta(minutes=20))]


# --- prompt-budget proof -----------------------------------------------------------


def test_worst_case_snapshot_fits_the_token_budget_after_trimming() -> None:
    """Worst case: `max_concurrent_runs`-many stalled runs, each with a long id, plus
    every failure/timeout/permission-denial count nonzero — `build_prompt` must still
    trim the result to fit `context_tokens - max_output_tokens`, the same proof
    `RecentJournalSource`'s own issue (#55) required for its source.
    """
    max_concurrent_runs = 10
    old_event = iso(_NOW - timedelta(seconds=9999))
    running = [
        _FakeRunningRun(f"run-{'x' * 32}-{i}", None, old_event)
        for i in range(max_concurrent_runs)
    ]
    probe = _FakeProbe(
        counts={"queued": 10, "running": max_concurrent_runs},
        running=running,
        outcomes=_FakeOutcomeCounts(failed=99, timeout=99, permission_denied=99),
        intake_paused=True,
    )
    source = OpencodeStateSource(
        probe, stall_seconds=300, recent_window_minutes=15, clock=_clock(_NOW)
    )

    budget_tokens = 8192 - 512  # the real `[heart]` default headroom
    prompt = build_prompt(
        list(source.collect()), budget_tokens=budget_tokens, token_count=len
    )

    assert prompt.tokens <= budget_tokens


def test_worst_case_snapshot_is_trimmed_rather_than_overflowing_a_tiny_budget() -> None:
    """The same worst-case snapshot as above, against a budget too small to hold it
    whole — `build_prompt` must still never exceed it, trimming lowest-priority
    items first (this source's own info items, then its fault items) rather than
    ever overflowing.
    """
    old_event = iso(_NOW - timedelta(seconds=9999))
    running = [
        _FakeRunningRun(f"run-{'x' * 32}-{i}", None, old_event) for i in range(10)
    ]
    probe = _FakeProbe(
        counts={"queued": 10, "running": 10},
        running=running,
        outcomes=_FakeOutcomeCounts(failed=99, timeout=99, permission_denied=99),
        intake_paused=True,
    )
    source = OpencodeStateSource(
        probe, stall_seconds=300, recent_window_minutes=15, clock=_clock(_NOW)
    )

    # The fixed template overhead `build_prompt` always pays, plus a little room —
    # not nearly enough to hold all three (long) items from the worst-case snapshot.
    empty_tokens = build_prompt([], budget_tokens=10_000, token_count=len).tokens
    tiny_budget = empty_tokens + 20

    prompt = build_prompt(
        list(source.collect()), budget_tokens=tiny_budget, token_count=len
    )

    assert prompt.tokens <= tiny_budget
    assert prompt.items_dropped > 0
