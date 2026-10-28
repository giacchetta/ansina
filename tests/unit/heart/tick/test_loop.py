from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import threading
from collections.abc import AsyncGenerator, Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from ansina.api.readiness import Readiness
from ansina.brain.events import BrainErrorClass, BrainErrorEvent
from ansina.config import load_settings
from ansina.heart.journal import BrainEscalationStatus, HeartJournalRepository
from ansina.heart.runtime import BaseHeartRuntime
from ansina.heart.tick.decision import TickDecision
from ansina.heart.tick.fault_injection import FaultInjector, FaultKind, FaultWindow
from ansina.heart.tick.journal_handler import JournalDecisionHandler
from ansina.heart.tick.loop import (
    CompositeDecisionHandler,
    DecisionHandler,
    LoggingDecisionHandler,
    TickLoop,
    _next_tick_number,
    build_tick_loop,
)
from ansina.heart.tick.snapshot import TickPrompt
from ansina.heart.tick.sources.daemon_state import DaemonStateSource
from ansina.heart.tick.sources.opencode_state import (
    OpencodeStateSource,
    RunningRunState,
)
from ansina.heart.tick.sources.recent_journal import RecentJournalSource
from ansina.storage.database import Database


class _FakeHeart(BaseHeartRuntime):
    """Mirrors `tests/unit/heart/test_runtime.py`'s fake: 1 token per character,
    already loaded, with a configurable reply and an optional hook run inside
    `_generate` (executes in the `anyio` worker thread, same as the real adapters).
    """

    def __init__(
        self,
        *,
        context_tokens: int = 1000,
        max_output_tokens: int = 50,
        reply: str = "idle",
        on_generate: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(
            context_tokens=context_tokens, max_output_tokens=max_output_tokens
        )
        self.load()
        self._reply = reply
        self._on_generate = on_generate
        self.generate_calls = 0

    def _load_backend(self) -> None:
        pass

    def _generate(self, prompt: str, max_tokens: int) -> str:
        self.generate_calls += 1
        if self._on_generate is not None:
            self._on_generate()
        return self._reply

    def _token_count(self, text: str) -> int:
        return len(text)

    def _unload_backend(self) -> None:
        pass


class _RecordingHandler:
    def __init__(self) -> None:
        self.calls: list[tuple[TickDecision, TickPrompt, int, float]] = []

    async def handle(
        self,
        decision: TickDecision,
        prompt: TickPrompt,
        *,
        tick_number: int,
        duration_seconds: float,
    ) -> None:
        self.calls.append((decision, prompt, tick_number, duration_seconds))


async def _run_briefly(loop: TickLoop, seconds: float) -> None:
    task = asyncio.create_task(loop.run())
    await asyncio.sleep(seconds)
    await loop.stop()
    await asyncio.wait_for(task, timeout=1)


# --- pure scheduling math: no asyncio, no real time, no flakiness -----------------


def test_next_tick_number_holds_steady_when_on_schedule() -> None:
    assert _next_tick_number(3, start=0.0, now=3.0, interval=1.0) == 3


def test_next_tick_number_holds_steady_when_tick_finishes_early() -> None:
    assert _next_tick_number(3, start=0.0, now=3.2, interval=1.0) == 3


def test_next_tick_number_catches_up_after_a_long_tick_without_bursting() -> None:
    # 5.5 intervals elapsed while tick 3 ran; the loop should catch up to the next
    # still-future slot (8), not queue ticks 4-8.
    assert _next_tick_number(3, start=0.0, now=8.5, interval=1.0) == 8


# --- construction guards ------------------------------------------------------------


def test_rejects_non_positive_interval() -> None:
    with pytest.raises(ValueError, match="interval_seconds"):
        TickLoop(_FakeHeart(), interval_seconds=0, max_output_tokens=10)


def test_rejects_negative_jitter() -> None:
    with pytest.raises(ValueError, match="jitter_seconds"):
        TickLoop(
            _FakeHeart(), interval_seconds=1, max_output_tokens=10, jitter_seconds=-1
        )


# --- tick_once: the unit of work -----------------------------------------------------


async def test_tick_once_runs_generate_and_records_the_outcome() -> None:
    heart = _FakeHeart(reply="act")
    handler = _RecordingHandler()
    loop = TickLoop(
        heart, interval_seconds=100, max_output_tokens=10, decision_handler=handler
    )

    outcome = await loop.tick_once()

    assert outcome.status == "ok"
    assert outcome.decision is TickDecision.ACT
    assert heart.generate_calls == 1
    assert loop.ticks_run == 1
    assert loop.last_decision is TickDecision.ACT
    assert loop.last_tick_at is not None
    assert loop.last_duration_seconds is not None
    assert len(handler.calls) == 1
    dispatched_decision, dispatched_prompt, dispatched_tick, dispatched_duration = (
        handler.calls[0]
    )
    assert dispatched_decision is TickDecision.ACT
    assert isinstance(dispatched_prompt, TickPrompt)
    # Dispatched *after* the loop's own bookkeeping (issue #55) — so the handler sees
    # the exact tick number/duration the "heart tick completed" log line also reports.
    assert dispatched_tick == loop.ticks_run == 1
    assert dispatched_duration == outcome.duration_seconds == loop.last_duration_seconds


async def test_tick_once_logs_decision_and_duration(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    loop = TickLoop(
        _FakeHeart(reply="idle"), interval_seconds=100, max_output_tokens=10
    )

    await loop.tick_once()

    lines = [
        line for line in captured_logs() if line["message"] == "heart tick completed"
    ]
    assert len(lines) == 1
    extra = lines[0]["extra"]
    assert extra["decision"] == "idle"
    assert "duration_seconds" in extra


async def test_tick_once_skips_when_another_tick_is_already_in_flight() -> None:
    started = threading.Event()
    release = threading.Event()

    def _block() -> None:
        started.set()
        release.wait(timeout=2)

    heart = _FakeHeart(reply="idle", on_generate=_block)
    loop = TickLoop(heart, interval_seconds=100, max_output_tokens=10)

    first = asyncio.create_task(loop.tick_once())
    await asyncio.to_thread(started.wait, 2)

    second = await loop.tick_once()

    release.set()
    first_outcome = await first

    assert second.status == "skipped"
    assert second.decision is None
    assert first_outcome.status == "ok"
    assert heart.generate_calls == 1


async def test_default_handler_logs_act_and_escalate(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    for reply in ("act", "escalate"):
        loop = TickLoop(
            _FakeHeart(reply=reply), interval_seconds=100, max_output_tokens=10
        )
        await loop.tick_once()

    lines = captured_logs()
    assert any("act decision" in line["message"] for line in lines)
    assert any(
        "escalate decision" in line["message"] and line["level"] == "WARNING"
        for line in lines
    )


def test_paused_property_reflects_pause_and_resume() -> None:
    loop = TickLoop(_FakeHeart(), interval_seconds=100, max_output_tokens=10)
    initially_paused = loop.paused

    loop.pause()
    paused_after_pause = loop.paused

    loop.resume()
    paused_after_resume = loop.paused

    assert initially_paused is False
    assert paused_after_pause is True
    assert paused_after_resume is False


# --- run(): scheduling, backpressure, kill switch, fault isolation ------------------


async def test_run_ticks_repeatedly_until_stopped() -> None:
    loop = TickLoop(_FakeHeart(), interval_seconds=0.02, max_output_tokens=10)

    await _run_briefly(loop, 0.1)

    assert loop.ticks_run >= 2


async def test_pause_stops_future_ticks_and_resume_restarts_them() -> None:
    loop = TickLoop(_FakeHeart(), interval_seconds=0.02, max_output_tokens=10)
    task = asyncio.create_task(loop.run())

    await asyncio.sleep(0.09)
    loop.pause()
    ticks_while_paused_start = loop.ticks_run
    await asyncio.sleep(0.09)
    ticks_after_pause = loop.ticks_run
    assert ticks_after_pause == ticks_while_paused_start

    loop.resume()
    await asyncio.sleep(0.09)
    ticks_after_resume = loop.ticks_run
    assert ticks_after_resume > ticks_while_paused_start

    await loop.stop()
    await asyncio.wait_for(task, timeout=1)


async def test_stop_ends_a_long_wait_promptly() -> None:
    loop = TickLoop(_FakeHeart(), interval_seconds=100, max_output_tokens=10)
    await loop.start()

    await asyncio.wait_for(loop.stop(), timeout=1)

    assert loop.ticks_run == 0


async def test_stop_before_start_does_not_hang() -> None:
    loop = TickLoop(_FakeHeart(), interval_seconds=100, max_output_tokens=10)

    await asyncio.wait_for(loop.stop(), timeout=1)


async def test_is_healthy_reflects_task_lifecycle() -> None:
    loop = TickLoop(_FakeHeart(), interval_seconds=100, max_output_tokens=10)

    assert not loop.is_healthy()

    await loop.start()
    assert loop.is_healthy()

    await loop.stop()
    assert not loop.is_healthy()


async def test_run_survives_a_raising_tick(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    class _BrokenHeart(_FakeHeart):
        def _generate(self, prompt: str, max_tokens: int) -> str:
            raise RuntimeError("backend exploded")

    loop = TickLoop(_BrokenHeart(), interval_seconds=0.02, max_output_tokens=10)
    task = asyncio.create_task(loop.run())

    await asyncio.sleep(0.07)
    assert not task.done()

    await loop.stop()
    await asyncio.wait_for(task, timeout=1)

    assert any(line["level"] == "ERROR" for line in captured_logs())
    assert loop.ticks_run == 0


# --- circuit breaker (issue #54) -----------------------------------------------------


class _FlakyHeart(_FakeHeart):
    """Fails on demand: raises for the first `fail_times` calls, then succeeds."""

    def __init__(self, *, fail_times: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fail_times = fail_times
        self._calls = 0

    def _generate(self, prompt: str, max_tokens: int) -> str:
        self._calls += 1
        if self._calls <= self._fail_times:
            raise RuntimeError("backend exploded")
        return self._reply


async def _drive_ticks(loop: TickLoop, times: int) -> None:
    """Mirrors `run()`'s own per-slot step (call `tick_once()`, and on a raised
    exception record the failure the same way `run()`'s swallow-and-log handler
    does) without its real-time scheduling wait — lets these tests exercise the
    breaker deterministically, with no real sleeping.
    """
    for _ in range(times):
        try:
            await loop.tick_once()
        except Exception:
            loop._record_failure()


def test_rejects_non_positive_max_consecutive_failures() -> None:
    with pytest.raises(ValueError, match="max_consecutive_failures"):
        TickLoop(
            _FakeHeart(),
            interval_seconds=1,
            max_output_tokens=10,
            max_consecutive_failures=0,
        )


def test_rejects_non_positive_overrun_ratio() -> None:
    with pytest.raises(ValueError, match="overrun_ratio"):
        TickLoop(
            _FakeHeart(), interval_seconds=1, max_output_tokens=10, overrun_ratio=0
        )


async def test_consecutive_failures_increment_and_reset_on_the_next_success() -> None:
    heart = _FlakyHeart(fail_times=2)
    loop = TickLoop(
        heart, interval_seconds=100, max_output_tokens=10, max_consecutive_failures=5
    )

    await _drive_ticks(loop, 3)  # fail, fail, succeed

    assert loop.failures_total == 2
    assert loop.consecutive_failures == 0
    assert loop.ticks_run == 1


async def test_failures_total_is_lifetime_and_survives_a_later_success() -> None:
    heart = _FlakyHeart(fail_times=1)
    loop = TickLoop(
        heart, interval_seconds=100, max_output_tokens=10, max_consecutive_failures=5
    )

    await _drive_ticks(loop, 4)  # fail, then three successes

    assert loop.failures_total == 1
    assert loop.consecutive_failures == 0


async def test_breaker_trips_after_exactly_max_consecutive_failures() -> None:
    heart = _FlakyHeart(fail_times=100)
    loop = TickLoop(
        heart, interval_seconds=100, max_output_tokens=10, max_consecutive_failures=3
    )

    await _drive_ticks(loop, 2)
    paused_before_trip = loop.paused
    assert paused_before_trip is False
    reason_before_trip = loop.pause_reason
    assert reason_before_trip is None

    await _drive_ticks(loop, 1)  # the 3rd consecutive failure trips the breaker

    assert loop.consecutive_failures == 3
    paused_after_trip = loop.paused
    assert paused_after_trip is True
    reason = loop.pause_reason
    assert reason is not None
    assert "3 consecutive tick failures" in reason
    # `heart_tick`'s readiness check means "the background task is alive," which
    # stays true regardless of pause state — `is_healthy()` never reflects a pause,
    # only whether `start()` has ever been called on this loop (it hasn't, here).
    assert loop.is_healthy() is False


async def test_breaker_trips_on_consecutive_overruns_independent_of_failures() -> None:
    """A slow-but-succeeding tick never touches `consecutive_failures` — the overrun
    gauge trips the breaker on its own. Duration is controlled by a scripted clock
    (two `self._clock()` reads per `tick_once()` call: start, then duration), never
    real sleeping.
    """
    clock_values = iter([0.0, 30.0, 30.0, 60.0, 60.0, 90.0])
    loop = TickLoop(
        _FakeHeart(reply="idle"),
        interval_seconds=10,
        max_output_tokens=10,
        max_consecutive_failures=3,
        overrun_ratio=0.8,  # threshold: duration >= 8.0
        clock=lambda: next(clock_values),
    )

    await _drive_ticks(loop, 2)
    assert loop.consecutive_overruns == 2
    paused_before_trip = loop.paused
    assert paused_before_trip is False

    await _drive_ticks(loop, 1)  # the 3rd consecutive overrun trips the breaker

    assert loop.consecutive_overruns == 3
    assert loop.consecutive_failures == 0  # an overrun is not a failure
    paused_after_trip = loop.paused
    assert paused_after_trip is True
    reason = loop.pause_reason
    assert reason is not None
    assert "3 consecutive tick overruns" in reason


async def test_a_fast_tick_resets_consecutive_overruns() -> None:
    clock_values = iter([0.0, 30.0, 30.0, 30.1])  # tick 1 overruns, tick 2 is fast
    loop = TickLoop(
        _FakeHeart(reply="idle"),
        interval_seconds=10,
        max_output_tokens=10,
        overrun_ratio=0.8,
        clock=lambda: next(clock_values),
    )

    await _drive_ticks(loop, 2)

    assert loop.consecutive_overruns == 0


async def test_auto_pause_disabled_counters_accrue_but_pause_never_fires() -> None:
    heart = _FlakyHeart(fail_times=100)
    loop = TickLoop(
        heart,
        interval_seconds=100,
        max_output_tokens=10,
        max_consecutive_failures=2,
        auto_pause_enabled=False,
    )

    await _drive_ticks(loop, 5)

    assert loop.consecutive_failures == 5
    assert loop.failures_total == 5
    assert loop.paused is False
    assert loop.pause_reason is None


async def test_resume_clears_pause_reason_and_gauges_and_ticking_resumes() -> None:
    heart = _FlakyHeart(fail_times=3)
    loop = TickLoop(
        heart, interval_seconds=100, max_output_tokens=10, max_consecutive_failures=3
    )

    await _drive_ticks(loop, 3)  # trips the breaker
    paused_before_resume = loop.paused
    assert paused_before_resume is True
    tripped_reason = loop.pause_reason
    assert tripped_reason is not None

    loop.resume()

    paused_after_resume = loop.paused
    assert paused_after_resume is False
    resumed_reason = loop.pause_reason
    assert resumed_reason is None
    assert loop.consecutive_failures == 0
    assert loop.consecutive_overruns == 0

    await _drive_ticks(loop, 1)  # the 4th call succeeds (fail_times=3)

    assert loop.ticks_run == 1


def test_pause_without_a_reason_is_the_manual_operator_kill_switch() -> None:
    loop = TickLoop(_FakeHeart(), interval_seconds=100, max_output_tokens=10)

    loop.pause()

    assert loop.paused is True
    assert loop.pause_reason is None


def test_add_source_registers_an_additional_snapshot_source() -> None:
    class _Source:
        name = "extra"

        def collect(self) -> list[Any]:
            return []

    loop = TickLoop(_FakeHeart(), interval_seconds=100, max_output_tokens=10)
    source = _Source()

    loop.add_source(source)

    assert list(loop._sources) == [source]


# --- per-decision counters (issue #68) -----------------------------------------------


async def test_idle_total_increments_on_an_idle_decision() -> None:
    loop = TickLoop(
        _FakeHeart(reply="idle"), interval_seconds=100, max_output_tokens=10
    )

    await loop.tick_once()

    assert loop.idle_total == 1
    assert loop.act_total == 0
    assert loop.escalate_total == 0


async def test_act_total_increments_on_an_act_decision() -> None:
    loop = TickLoop(_FakeHeart(reply="act"), interval_seconds=100, max_output_tokens=10)

    await loop.tick_once()

    assert loop.idle_total == 0
    assert loop.act_total == 1
    assert loop.escalate_total == 0


async def test_escalate_total_increments_on_an_escalate_decision() -> None:
    loop = TickLoop(
        _FakeHeart(reply="escalate", context_tokens=8192, max_output_tokens=512),
        interval_seconds=100,
        max_output_tokens=512,
    )

    await loop.tick_once()

    assert loop.idle_total == 0
    assert loop.act_total == 0
    assert loop.escalate_total == 1


async def test_per_decision_counters_are_lifetime_totals_across_many_ticks() -> None:
    """Each counter accrues independently across ticks — the exact property that
    lets a telemetry reader reconstruct a window's decision distribution by
    diffing two samples' totals, rather than inferring it from `last_decision`
    alone.
    """
    replies = ["idle", "idle", "act", "idle", "escalate", "act"]
    calls = {"n": 0}

    class _ScriptedHeart(_FakeHeart):
        def _generate(self, prompt: str, max_tokens: int) -> str:
            reply = replies[calls["n"]]
            calls["n"] += 1
            self.generate_calls += 1
            return reply

    loop = TickLoop(_ScriptedHeart(), interval_seconds=100, max_output_tokens=10)

    for _ in replies:
        await loop.tick_once()

    assert loop.idle_total == 3
    assert loop.act_total == 2
    assert loop.escalate_total == 1
    assert loop.ticks_run == len(replies)


async def test_per_decision_counters_are_not_reset_by_resume() -> None:
    """Lifetime, like `failures_total` — not cleared by `resume()`, unlike the two
    *consecutive* breaker gauges.
    """
    loop = TickLoop(
        _FakeHeart(reply="idle"), interval_seconds=100, max_output_tokens=10
    )

    await loop.tick_once()
    loop.pause()
    loop.resume()

    assert loop.idle_total == 1


# --- the default decision handler and factory ---------------------------------------


async def test_logging_decision_handler_conforms_to_the_protocol() -> None:
    handler: DecisionHandler = LoggingDecisionHandler()
    prompt = TickPrompt(text="x", tokens=1, items_included=0, items_dropped=0)

    # no-op, must not raise
    await handler.handle(TickDecision.IDLE, prompt, tick_number=1, duration_seconds=0.1)


def test_build_tick_loop_wires_settings_into_the_loop(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch, db: Database
) -> None:
    """`db` (this directory's `conftest.py`) is a real, migrated `Database` —
    `build_tick_loop` (issue #55) now constructs a `HeartJournalRepository` eagerly,
    which needs an actual connection, not just the `is_healthy()` surface a bare
    `HealthProbe` double would offer.
    """
    monkeypatch.setenv("ANSINA_HEART__TICK__INTERVAL_SECONDS", "12.5")
    monkeypatch.setenv("ANSINA_HEART__TICK__JITTER_SECONDS", "1.5")
    monkeypatch.setenv("ANSINA_HEART__MAX_OUTPUT_TOKENS", "77")
    monkeypatch.setenv("ANSINA_HEART__TICK__MAX_CONSECUTIVE_FAILURES", "9")
    monkeypatch.setenv("ANSINA_HEART__TICK__OVERRUN_RATIO", "0.5")
    monkeypatch.setenv("ANSINA_HEART__TICK__AUTO_PAUSE_ENABLED", "false")
    settings = load_settings()
    heart = _FakeHeart()

    loop = build_tick_loop(
        settings, heart, db=db, readiness=Readiness(), brain=None, opencode=None
    )

    assert loop._interval == 12.5
    assert loop._max_output_tokens == 77
    assert loop._max_consecutive_failures == 9
    assert loop._overrun_ratio == 0.5
    assert loop._auto_pause_enabled is False


def test_build_tick_loop_uses_journal_decision_handler_by_default(
    clean_env: None, tmp_cwd: Path, db: Database
) -> None:
    heart = _FakeHeart()
    settings = load_settings()

    loop = build_tick_loop(
        settings, heart, db=db, readiness=Readiness(), brain=None, opencode=None
    )

    assert isinstance(loop._handler, JournalDecisionHandler)


def test_build_tick_loop_registers_the_daemon_state_and_recent_journal_sources(
    clean_env: None, tmp_cwd: Path, db: Database
) -> None:
    heart = _FakeHeart()
    settings = load_settings()

    loop = build_tick_loop(
        settings, heart, db=db, readiness=Readiness(), brain=None, opencode=None
    )

    assert len(loop._sources) == 2
    assert isinstance(loop._sources[0], DaemonStateSource)
    assert isinstance(loop._sources[1], RecentJournalSource)


@dataclasses.dataclass(frozen=True, slots=True)
class _FakeOutcomeCounts:
    failed: int = 0
    timeout: int = 0
    permission_denied: int = 0


class _FakeOpencodeProbe:
    """A minimal `OpencodeStateProbe` double — satisfies the Protocol structurally.
    Declared return types reference the Protocols themselves
    (`RunningRunState`/`OutcomeCounts`, both from `ansina.heart`), never a concrete
    `ansina.opencode` type — this test file stays on the `ansina.heart` side of the
    boundary, same as the probe's own module.
    """

    intake_paused = False

    def counts_by_status(self) -> dict[str, int]:
        return {}

    def running_runs(self) -> Sequence[RunningRunState]:
        return []

    def recent_outcome_counts(self, *, since: str) -> _FakeOutcomeCounts:
        return _FakeOutcomeCounts()


def test_build_tick_loop_registers_no_opencode_source_when_hands_are_off(
    clean_env: None, tmp_cwd: Path, db: Database
) -> None:
    """AC: the Heart-on/Hands-off registered-source list must be unchanged — the
    exact same two sources (`DaemonStateSource`/`RecentJournalSource`), not merely
    an equivalent-looking three.
    """
    heart = _FakeHeart()
    settings = load_settings()

    loop = build_tick_loop(
        settings, heart, db=db, readiness=Readiness(), brain=None, opencode=None
    )

    assert len(loop._sources) == 2


def test_build_tick_loop_registers_the_opencode_state_source_when_hands_exist(
    clean_env: None, tmp_cwd: Path, db: Database
) -> None:
    heart = _FakeHeart()
    settings = load_settings()

    loop = build_tick_loop(
        settings,
        heart,
        db=db,
        readiness=Readiness(),
        brain=None,
        opencode=_FakeOpencodeProbe(),
    )

    assert len(loop._sources) == 3
    assert isinstance(loop._sources[2], OpencodeStateSource)


def test_build_tick_loop_wires_opencode_thresholds_from_settings(
    clean_env: None, tmp_cwd: Path, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_HEART__TICK__OPENCODE_STALL_SECONDS", "123")
    monkeypatch.setenv("ANSINA_HEART__TICK__OPENCODE_RECENT_WINDOW_MINUTES", "7")
    heart = _FakeHeart()
    settings = load_settings()

    loop = build_tick_loop(
        settings,
        heart,
        db=db,
        readiness=Readiness(),
        brain=None,
        opencode=_FakeOpencodeProbe(),
    )

    source = loop._sources[2]
    assert isinstance(source, OpencodeStateSource)
    assert source._stall_seconds == 123.0
    assert source._recent_window_minutes == 7


# --- CompositeDecisionHandler (issue #64) -------------------------------------------


async def test_composite_decision_handler_calls_every_handler_in_order() -> None:
    first = _RecordingHandler()
    second = _RecordingHandler()
    composite = CompositeDecisionHandler((first, second))
    prompt = TickPrompt(text="x", tokens=1, items_included=0, items_dropped=0)

    await composite.handle(
        TickDecision.ESCALATE, prompt, tick_number=5, duration_seconds=0.2
    )

    assert first.calls == [(TickDecision.ESCALATE, prompt, 5, 0.2)]
    assert second.calls == [(TickDecision.ESCALATE, prompt, 5, 0.2)]


async def test_composite_decision_handler_stops_at_the_first_raising_handler() -> None:
    class _RaisingHandler:
        async def handle(
            self,
            decision: TickDecision,
            prompt: TickPrompt,
            *,
            tick_number: int,
            duration_seconds: float,
        ) -> None:
            raise RuntimeError("boom")

    second = _RecordingHandler()
    composite = CompositeDecisionHandler((_RaisingHandler(), second))
    prompt = TickPrompt(text="x", tokens=1, items_included=0, items_dropped=0)

    with pytest.raises(RuntimeError, match="boom"):
        await composite.handle(
            TickDecision.IDLE, prompt, tick_number=1, duration_seconds=0.1
        )

    assert second.calls == []


# --- build_tick_loop: escalate_to_brain gating (issue #64) --------------------------


def test_build_tick_loop_escalate_to_brain_defaults_false_bare_journal_handler(
    clean_env: None, tmp_cwd: Path, db: Database
) -> None:
    """`escalate_to_brain = false` (the default) must leave `decision_handler` as the
    *exact* `JournalDecisionHandler` instance this function has always built — not
    merely equivalent behavior behind a branch — which is what makes the flag a
    verified no-op rather than an assumed one.
    """
    heart = _FakeHeart()
    settings = load_settings()

    loop = build_tick_loop(
        settings, heart, db=db, readiness=Readiness(), brain=None, opencode=None
    )

    assert isinstance(loop._handler, JournalDecisionHandler)
    assert not isinstance(loop._handler, CompositeDecisionHandler)


def test_build_tick_loop_escalate_to_brain_true_composes_brain_handler(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch, db: Database
) -> None:
    monkeypatch.setenv("ANSINA_HEART__TICK__ESCALATE_TO_BRAIN", "true")
    heart = _FakeHeart()
    settings = load_settings()

    loop = build_tick_loop(
        settings, heart, db=db, readiness=Readiness(), brain=None, opencode=None
    )

    assert isinstance(loop._handler, CompositeDecisionHandler)


async def test_escalate_to_brain_false_never_calls_the_brain_end_to_end(
    clean_env: None, tmp_cwd: Path, db: Database
) -> None:
    """End-to-end proof of the "verified no-op" claim: even a `TickLoop` that
    actually decides `escalate` for real never touches a configured `BrainProvider`
    when the flag is off.
    """

    class _FailingBrainProvider:
        def stream(self, request: object) -> AsyncGenerator[object]:
            raise AssertionError("BrainProvider.stream must never be called")

        async def aclose(self) -> None:
            pass

    heart = _FakeHeart(reply="escalate", context_tokens=8192, max_output_tokens=512)
    settings = load_settings()

    loop = build_tick_loop(
        settings,
        heart,
        db=db,
        readiness=Readiness(),
        brain=_FailingBrainProvider(),  # type: ignore[arg-type]
        opencode=None,
    )

    outcome = await loop.tick_once()

    assert outcome.decision is TickDecision.ESCALATE
    # Only one row: JournalDecisionHandler's own, no second Brain-outcome row.
    repository = HeartJournalRepository(db)
    entries = repository.list_recent(limit=10)
    assert len(entries) == 1
    assert entries[0].brain_status is None


async def test_escalate_to_brain_true_journals_two_rows_and_never_trips_the_breaker(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch, db: Database
) -> None:
    """A simulated Brain failure must not pause the loop or increment its failure
    counters (issue #64's own AC) — a different failure domain from a tick fault.
    """
    monkeypatch.setenv("ANSINA_HEART__TICK__ESCALATE_TO_BRAIN", "true")

    class _ErroringBrainProvider:
        def stream(self, request: object) -> AsyncGenerator[object]:
            async def _gen() -> AsyncGenerator[object]:
                yield BrainErrorEvent(
                    error_class=BrainErrorClass.PROVIDER_SERVER,
                    message="simulated failure",
                    retryable=True,
                )

            return _gen()

        async def aclose(self) -> None:
            pass

    heart = _FakeHeart(reply="escalate", context_tokens=8192, max_output_tokens=512)
    settings = load_settings()

    loop = build_tick_loop(
        settings,
        heart,
        db=db,
        readiness=Readiness(),
        brain=_ErroringBrainProvider(),  # type: ignore[arg-type]
        opencode=None,
    )

    outcome = await loop.tick_once()

    assert outcome.decision is TickDecision.ESCALATE
    repository = HeartJournalRepository(db)
    entries = repository.list_recent(limit=10)
    assert len(entries) == 2
    brain_rows = [entry for entry in entries if entry.brain_status is not None]
    assert len(brain_rows) == 1
    assert brain_rows[0].brain_status == BrainEscalationStatus.ERROR
    assert loop.failures_total == 0
    assert loop.consecutive_failures == 0
    assert loop.paused is False


# --- build_tick_loop: fault injection wiring (issue #72) ----------------------------


def _write_schedule(path: Path, windows: list[dict[str, object]]) -> None:
    path.write_text(json.dumps(windows), encoding="utf-8")


def test_build_tick_loop_fault_injection_disabled_by_default_is_a_noop(
    clean_env: None, tmp_cwd: Path, db: Database
) -> None:
    """`[heart.tick.fault_injection] enabled = false` (the default) must leave the
    exact object graph `build_tick_loop` has always built — no third source, no
    registered readiness check, the bare `JournalDecisionHandler` instance.
    """
    heart = _FakeHeart()
    settings = load_settings()
    readiness = Readiness()

    loop = build_tick_loop(
        settings, heart, db=db, readiness=readiness, brain=None, opencode=None
    )

    assert isinstance(loop._handler, JournalDecisionHandler)
    assert not isinstance(loop._handler, CompositeDecisionHandler)
    assert len(loop._sources) == 2
    assert "fault_injection" not in readiness.snapshot()


def test_build_tick_loop_fault_injection_enabled_composes_injector(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch, db: Database
) -> None:
    schedule_path = tmp_cwd / "schedule.json"
    _write_schedule(
        schedule_path,
        [
            {
                "id": "x",
                "fault": "readiness_check",
                "at_tick": 1,
                "duration_ticks": 5,
                "expected_decision": "escalate",
            }
        ],
    )
    monkeypatch.setenv("ANSINA_HEART__TICK__FAULT_INJECTION__ENABLED", "true")
    monkeypatch.setenv(
        "ANSINA_HEART__TICK__FAULT_INJECTION__SCHEDULE_PATH", str(schedule_path)
    )
    heart = _FakeHeart()
    settings = load_settings()
    readiness = Readiness()

    loop = build_tick_loop(
        settings, heart, db=db, readiness=readiness, brain=None, opencode=None
    )

    assert isinstance(loop._handler, CompositeDecisionHandler)
    assert len(loop._sources) == 3
    assert isinstance(loop._sources[2], FaultInjector)
    # Registered fresh, read live — the window covers tick 1, the upcoming tick with
    # no ticks run yet, so the check is already failing the moment it's registered.
    assert readiness.snapshot()["fault_injection"] is False


def test_build_tick_loop_fault_injection_composes_after_the_brain_handler(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch, db: Database
) -> None:
    """Order matters: the injected failure must be recorded as the tick's own
    outcome, never evidence it never happened — `JournalDecisionHandler` always runs
    first, `FaultInjector` always runs last.
    """
    schedule_path = tmp_cwd / "schedule.json"
    _write_schedule(schedule_path, [])
    monkeypatch.setenv("ANSINA_HEART__TICK__ESCALATE_TO_BRAIN", "true")
    monkeypatch.setenv("ANSINA_HEART__TICK__FAULT_INJECTION__ENABLED", "true")
    monkeypatch.setenv(
        "ANSINA_HEART__TICK__FAULT_INJECTION__SCHEDULE_PATH", str(schedule_path)
    )
    heart = _FakeHeart()
    settings = load_settings()

    loop = build_tick_loop(
        settings, heart, db=db, readiness=Readiness(), brain=None, opencode=None
    )

    assert isinstance(loop._handler, CompositeDecisionHandler)
    handlers = loop._handler._handlers
    assert len(handlers) == 3
    assert isinstance(handlers[0], JournalDecisionHandler)
    assert isinstance(handlers[2], FaultInjector)


def test_build_tick_loop_warns_when_a_fault_window_may_trip_the_breaker(
    clean_env: None,
    tmp_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
    caplog: pytest.LogCaptureFixture,
) -> None:
    schedule_path = tmp_cwd / "schedule.json"
    _write_schedule(
        schedule_path,
        [
            {
                "id": "long-failure",
                "fault": "tick_failure",
                "at_tick": 1,
                "duration_ticks": 5,
                "expected_decision": "escalate",
            }
        ],
    )
    monkeypatch.setenv("ANSINA_HEART__TICK__FAULT_INJECTION__ENABLED", "true")
    monkeypatch.setenv(
        "ANSINA_HEART__TICK__FAULT_INJECTION__SCHEDULE_PATH", str(schedule_path)
    )
    monkeypatch.setenv("ANSINA_HEART__TICK__MAX_CONSECUTIVE_FAILURES", "5")
    heart = _FakeHeart()
    settings = load_settings()

    with caplog.at_level(logging.WARNING):
        build_tick_loop(
            settings, heart, db=db, readiness=Readiness(), brain=None, opencode=None
        )

    assert any(
        "may trip the circuit breaker" in record.message for record in caplog.records
    )


def test_build_tick_loop_does_not_warn_when_auto_pause_is_disabled(
    clean_env: None,
    tmp_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
    caplog: pytest.LogCaptureFixture,
) -> None:
    schedule_path = tmp_cwd / "schedule.json"
    _write_schedule(
        schedule_path,
        [
            {
                "id": "long-failure",
                "fault": "tick_failure",
                "at_tick": 1,
                "duration_ticks": 5,
                "expected_decision": "escalate",
            }
        ],
    )
    monkeypatch.setenv("ANSINA_HEART__TICK__FAULT_INJECTION__ENABLED", "true")
    monkeypatch.setenv(
        "ANSINA_HEART__TICK__FAULT_INJECTION__SCHEDULE_PATH", str(schedule_path)
    )
    monkeypatch.setenv("ANSINA_HEART__TICK__MAX_CONSECUTIVE_FAILURES", "5")
    monkeypatch.setenv("ANSINA_HEART__TICK__AUTO_PAUSE_ENABLED", "false")
    heart = _FakeHeart()
    settings = load_settings()

    with caplog.at_level(logging.WARNING):
        build_tick_loop(
            settings, heart, db=db, readiness=Readiness(), brain=None, opencode=None
        )

    assert not any(
        "may trip the circuit breaker" in record.message for record in caplog.records
    )


async def test_fault_injection_end_to_end_prompt_reports_a_real_readiness_failure(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch, db: Database
) -> None:
    """Proves the module's own central claim: the injected fault is reported by
    `DaemonStateSource` exactly the way it reports a real one, because — as far as
    that source can tell — it is one. No fabricated snapshot text anywhere.
    """
    schedule_path = tmp_cwd / "schedule.json"
    _write_schedule(
        schedule_path,
        [
            {
                "id": "stuck-sensor",
                "fault": "readiness_check",
                "at_tick": 1,
                "duration_ticks": 1,
                "expected_decision": "escalate",
            }
        ],
    )
    monkeypatch.setenv("ANSINA_HEART__TICK__FAULT_INJECTION__ENABLED", "true")
    monkeypatch.setenv(
        "ANSINA_HEART__TICK__FAULT_INJECTION__SCHEDULE_PATH", str(schedule_path)
    )
    captured_prompts: list[str] = []

    class _CapturingHeart(_FakeHeart):
        def _generate(self, prompt: str, max_tokens: int) -> str:
            captured_prompts.append(prompt)
            return super()._generate(prompt, max_tokens)

    heart = _CapturingHeart(context_tokens=8192, max_output_tokens=512)
    settings = load_settings()

    loop = build_tick_loop(
        settings, heart, db=db, readiness=Readiness(), brain=None, opencode=None
    )
    await loop.tick_once()

    assert len(captured_prompts) == 1
    assert "fault_injection" in captured_prompts[0]


# --- FaultInjector wired directly into TickLoop: real counter effects (issue #72) ---


async def test_fault_injector_tick_failure_bumps_real_failure_counters_end_to_end() -> (
    None
):
    """The `DecisionHandler` role really does propagate out of `tick_once()` into
    `run()`'s own swallow-and-log handler — `_drive_ticks` mirrors that handler the
    same way the circuit-breaker tests above do.
    """
    window = FaultWindow(
        id="recurring",
        fault=FaultKind.TICK_FAILURE,
        at_tick=2,
        duration_ticks=2,
        expected_decision=TickDecision.ESCALATE,
    )
    injector = FaultInjector((window,))
    heart = _FakeHeart(reply="idle")
    loop = TickLoop(
        heart,
        interval_seconds=100,
        max_output_tokens=10,
        decision_handler=CompositeDecisionHandler((_RecordingHandler(), injector)),
        max_consecutive_failures=10,
    )
    injector.bind(loop)

    await _drive_ticks(loop, 4)  # ticks 1(ok) 2(forced) 3(forced) 4(ok)

    assert loop.ticks_run == 4
    assert loop.failures_total == 2
    assert loop.consecutive_failures == 0


async def test_fault_injector_tick_overrun_role_triggers_a_real_measured_overrun() -> (
    None
):
    """The `collect()` role doesn't fabricate the overrun itself — `TickLoop` still
    measures real elapsed time (scripted here, the same way
    `test_breaker_trips_on_consecutive_overruns_independent_of_failures` avoids real
    sleeping) and the counter it bumps is `TickLoop`'s own, never set directly by
    `FaultInjector`.
    """
    sleeps: list[float] = []
    window = FaultWindow(
        id="slow",
        fault=FaultKind.TICK_OVERRUN,
        at_tick=1,
        duration_ticks=1,
        expected_decision=TickDecision.ESCALATE,
        sleep_seconds=42.0,
    )
    injector = FaultInjector((window,), sleep=sleeps.append)
    clock_values = iter([0.0, 30.0])  # duration 30s >= overrun_ratio * interval (8.0)
    loop = TickLoop(
        _FakeHeart(reply="idle"),
        interval_seconds=10,
        max_output_tokens=10,
        overrun_ratio=0.8,
        clock=lambda: next(clock_values),
    )
    injector.bind(loop)
    loop.add_source(injector)

    await _drive_ticks(loop, 1)

    assert sleeps == [42.0]  # the role was actually invoked for the active window
    assert loop.consecutive_overruns == 1  # TickLoop's own real measurement


async def test_fault_injector_readiness_role_reflects_window_boundaries() -> None:
    window = FaultWindow(
        id="stuck-sensor",
        fault=FaultKind.READINESS_CHECK,
        at_tick=2,
        duration_ticks=1,
        expected_decision=TickDecision.ESCALATE,
    )
    injector = FaultInjector((window,))
    loop = TickLoop(
        _FakeHeart(reply="idle"),
        interval_seconds=100,
        max_output_tokens=10,
    )
    injector.bind(loop)

    assert injector.readiness_ok() is True  # upcoming tick 1, before the window

    await _drive_ticks(loop, 1)

    assert injector.readiness_ok() is False  # upcoming tick 2, inside the window

    await _drive_ticks(loop, 1)

    assert injector.readiness_ok() is True  # upcoming tick 3, after the window
