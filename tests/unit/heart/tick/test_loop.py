from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ansina.api.readiness import Readiness
from ansina.config import load_settings
from ansina.heart.runtime import BaseHeartRuntime
from ansina.heart.tick.decision import TickDecision
from ansina.heart.tick.loop import (
    DecisionHandler,
    LoggingDecisionHandler,
    TickLoop,
    _next_tick_number,
    build_tick_loop,
)
from ansina.heart.tick.snapshot import TickPrompt
from ansina.heart.tick.sources.daemon_state import DaemonStateSource


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
        self.calls: list[tuple[TickDecision, TickPrompt]] = []

    def handle(self, decision: TickDecision, prompt: TickPrompt) -> None:
        self.calls.append((decision, prompt))


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
    dispatched_decision, dispatched_prompt = handler.calls[0]
    assert dispatched_decision is TickDecision.ACT
    assert isinstance(dispatched_prompt, TickPrompt)


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


# --- the default decision handler and factory ---------------------------------------


def test_logging_decision_handler_conforms_to_the_protocol() -> None:
    handler: DecisionHandler = LoggingDecisionHandler()
    prompt = TickPrompt(text="x", tokens=1, items_included=0, items_dropped=0)

    handler.handle(TickDecision.IDLE, prompt)  # no-op, must not raise


class _FakeDatabase:
    def is_healthy(self) -> bool:
        return True


def test_build_tick_loop_wires_settings_into_the_loop(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_HEART__TICK__INTERVAL_SECONDS", "12.5")
    monkeypatch.setenv("ANSINA_HEART__TICK__JITTER_SECONDS", "1.5")
    monkeypatch.setenv("ANSINA_HEART__MAX_OUTPUT_TOKENS", "77")
    monkeypatch.setenv("ANSINA_HEART__TICK__MAX_CONSECUTIVE_FAILURES", "9")
    monkeypatch.setenv("ANSINA_HEART__TICK__OVERRUN_RATIO", "0.5")
    monkeypatch.setenv("ANSINA_HEART__TICK__AUTO_PAUSE_ENABLED", "false")
    settings = load_settings()
    heart = _FakeHeart()

    loop = build_tick_loop(settings, heart, db=_FakeDatabase(), readiness=Readiness())

    assert loop._interval == 12.5
    assert loop._max_output_tokens == 77
    assert loop._max_consecutive_failures == 9
    assert loop._overrun_ratio == 0.5
    assert loop._auto_pause_enabled is False


def test_build_tick_loop_registers_exactly_one_daemon_state_source(
    clean_env: None, tmp_cwd: Path
) -> None:
    heart = _FakeHeart()
    settings = load_settings()

    loop = build_tick_loop(settings, heart, db=_FakeDatabase(), readiness=Readiness())

    assert len(loop._sources) == 1
    assert isinstance(loop._sources[0], DaemonStateSource)
