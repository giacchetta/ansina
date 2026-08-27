from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

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


# --- the default decision handler and factory ---------------------------------------


def test_logging_decision_handler_conforms_to_the_protocol() -> None:
    handler: DecisionHandler = LoggingDecisionHandler()
    prompt = TickPrompt(text="x", tokens=1, items_included=0, items_dropped=0)

    handler.handle(TickDecision.IDLE, prompt)  # no-op, must not raise


def test_build_tick_loop_wires_settings_into_the_loop(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_HEART__TICK__INTERVAL_SECONDS", "12.5")
    monkeypatch.setenv("ANSINA_HEART__TICK__JITTER_SECONDS", "1.5")
    monkeypatch.setenv("ANSINA_HEART__MAX_OUTPUT_TOKENS", "77")
    settings = load_settings()
    heart = _FakeHeart()

    loop = build_tick_loop(settings, heart)

    assert loop._interval == 12.5
    assert loop._max_output_tokens == 77
