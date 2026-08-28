"""`TickLoop` — the autonomic tick loop itself. See issue #11.

Every `interval_seconds` (plus jitter), the loop builds a bounded snapshot
(`heart.tick.snapshot`), calls the Heart to decide idle/act/escalate
(`heart.tick.decision`), and dispatches the decision to a `DecisionHandler`. Nothing
before this module ever called `HeartRuntime.generate()` — this is the first consumer.
"""

from __future__ import annotations

import asyncio
import functools
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import anyio.to_thread

from ansina.config.settings import Settings
from ansina.heart.runtime import HeartRuntime
from ansina.heart.tick.decision import TickDecision, parse_decision
from ansina.heart.tick.snapshot import (
    StateSnapshotSource,
    TickPrompt,
    build_prompt,
    collect_items,
)
from ansina.logging import get_logger

logger = get_logger(__name__)

Clock = Callable[[], float]
Jitter = Callable[[], float]


class DecisionHandler(Protocol):
    """What happens after a tick decides. Structural, like `HeartRuntime`."""

    def handle(self, decision: TickDecision, prompt: TickPrompt) -> None: ...


class TickLifecycle(Protocol):
    """The minimal surface `create_app`'s lifespan needs: start, stop, health.

    Structural, like `HeartRuntime` — `TickLoop` is the only real implementation, but
    keeping the dependency structural (rather than naming `TickLoop` directly) lets a
    lifespan test inject a lightweight double instead of the real scheduler.
    """

    def is_healthy(self) -> bool: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class TickController(TickLifecycle, Protocol):
    """The fuller surface `api/routes/heart.py` depends on: lifecycle plus the kill
    switch and the status fields `GET /heart/tick` reports.
    """

    @property
    def paused(self) -> bool: ...
    @property
    def ticks_run(self) -> int: ...
    @property
    def last_decision(self) -> TickDecision | None: ...
    @property
    def last_tick_at(self) -> str | None: ...
    @property
    def last_duration_seconds(self) -> float | None: ...

    def pause(self) -> None: ...
    def resume(self) -> None: ...


class LoggingDecisionHandler:
    """The shipped default: every decision is logged, nothing is dispatched anywhere.

    `act` has nothing to act on yet, and `escalate` has no `BrainProvider` to hand off
    to (issue #12) — logging is the only honest behavior until those land. `idle` gets
    no extra log line here; `TickLoop.tick_once` already logs every tick's decision.
    """

    def handle(self, decision: TickDecision, prompt: TickPrompt) -> None:
        if decision is TickDecision.ACT:
            logger.info(
                "heart tick: act decision (no action handler wired yet)",
                extra={"prompt_tokens": prompt.tokens},
            )
        elif decision is TickDecision.ESCALATE:
            logger.warning(
                "heart tick: escalate decision but no BrainProvider is wired yet "
                "(issue #12)",
                extra={"prompt_tokens": prompt.tokens},
            )


@dataclass(frozen=True, slots=True)
class TickOutcome:
    """The result of one `tick_once()` call.

    `status` is `"ok"` for a completed tick or `"skipped"` when backpressure refused a
    tick that arrived while another was still in flight.
    """

    status: str
    decision: TickDecision | None = None
    duration_seconds: float = 0.0


def _next_tick_number(
    tick_number: int, *, start: float, now: float, interval: float
) -> int:
    """The tick slot to run next, catching up past any deadlines already elapsed.

    A tick that overran one or more `interval_seconds` slots does not get a burst of
    catch-up ticks — this simply advances the counter to the next slot that is still in
    the future, the same way a cron-style scheduler drops missed runs instead of queuing
    them. Pure function, no I/O, so the "no unbounded drift" property is unit-testable
    without any real waiting.
    """
    n = tick_number
    while start + (n + 1) * interval <= now:
        n += 1
    return n


class TickLoop:
    """Schedules `tick_once()` at a fixed cadence, forever, until `stop()`.

    - **Backpressure**: `tick_once()` is guarded by `_in_flight`; a call made while
      another is running returns `TickOutcome(status="skipped")` immediately instead of
      queuing or overlapping.
    - **Drift**: each slot's deadline is `start + n * interval_seconds`, computed fresh
      every cycle (`_next_tick_number`) rather than `interval_seconds` after the
      previous tick returns — a slow tick shortens or skips its own next wait instead of
      shifting every later tick.
    - **Jitter**: `jitter_seconds` of uniform random delay is added to *when* the loop
      wakes for a slot, without perturbing the slot schedule itself.
    - **Kill switch**: `pause()`/`resume()` stop/restart future ticks without touching
      the running `asyncio.Task` — no process restart required.
    - **Fault isolation**: an exception from `tick_once()` (a backend failure, a bug) is
      logged and swallowed by `run()` — one bad tick must never end the always-on loop.
    """

    def __init__(
        self,
        heart: HeartRuntime,
        *,
        interval_seconds: float,
        max_output_tokens: int,
        jitter_seconds: float = 0.0,
        snapshot_sources: Sequence[StateSnapshotSource] = (),
        decision_handler: DecisionHandler | None = None,
        clock: Clock = time.monotonic,
        jitter: Jitter | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        if jitter_seconds < 0:
            raise ValueError("jitter_seconds must be >= 0")
        self._heart = heart
        self._interval = interval_seconds
        self._max_output_tokens = max_output_tokens
        self._sources = tuple(snapshot_sources)
        self._handler = decision_handler or LoggingDecisionHandler()
        self._clock = clock
        self._jitter = jitter or (lambda: random.uniform(0.0, jitter_seconds))

        self._in_flight = False
        self._paused = False
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._ticks_run = 0
        self._last_decision: TickDecision | None = None
        self._last_tick_at: str | None = None
        self._last_duration_seconds: float | None = None

    @property
    def ticks_run(self) -> int:
        return self._ticks_run

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def last_decision(self) -> TickDecision | None:
        """The most recently completed tick's decision, or `None` before the first."""
        return self._last_decision

    @property
    def last_tick_at(self) -> str | None:
        """UTC ISO 8601 timestamp of the most recently completed tick, or `None`."""
        return self._last_tick_at

    @property
    def last_duration_seconds(self) -> float | None:
        """The most recently completed tick's wall-clock duration, or `None`."""
        return self._last_duration_seconds

    def pause(self) -> None:
        """Kill switch: future ticks stop firing. Idempotent."""
        self._paused = True

    def resume(self) -> None:
        """Undo `pause()`. Idempotent."""
        self._paused = False

    def is_healthy(self) -> bool:
        """`True` iff the background task exists and hasn't exited — feeds a
        `Readiness` check the same way `HeartRuntime.is_healthy`/`Database.is_healthy`
        do.
        """
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the background task. Call once, from the lifespan."""
        self._stop_event.clear()
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        """Signal `run()` to exit and wait for it. Safe to call before `start()`."""
        self._stop_event.set()
        if self._task is not None:
            await self._task

    async def run(self) -> None:
        """The scheduling loop. Runs until `stop()`; see the class docstring."""
        start = self._clock()
        tick_number = 0
        while not self._stop_event.is_set():
            tick_number += 1
            deadline = start + tick_number * self._interval
            delay = max(0.0, deadline - self._clock()) + self._jitter()
            if await self._wait_or_stop(delay):
                break
            if not self._paused:
                try:
                    await self.tick_once()
                except Exception:
                    logger.exception("heart tick: unhandled error, continuing")
            tick_number = _next_tick_number(
                tick_number,
                start=start,
                now=self._clock(),
                interval=self._interval,
            )

    async def _wait_or_stop(self, delay: float) -> bool:
        """Waits up to `delay` seconds, returning `True` early iff `stop()` fired."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            return True
        except TimeoutError:
            return False

    async def tick_once(self) -> TickOutcome:
        """Run exactly one tick: snapshot -> generate -> parse -> dispatch -> log.

        Returns `TickOutcome(status="skipped")` without doing any work if another tick
        is already in flight — this is what makes overlap structurally impossible even
        if `run()` and a manual/administrative call race.
        """
        if self._in_flight:
            return TickOutcome(status="skipped")
        self._in_flight = True
        start = self._clock()
        try:
            items = collect_items(self._sources)
            budget_tokens = max(0, self._heart.context_tokens - self._max_output_tokens)
            prompt = build_prompt(
                items,
                budget_tokens=budget_tokens,
                token_count=self._heart.token_count,
            )
            raw = await anyio.to_thread.run_sync(
                functools.partial(
                    self._heart.generate,
                    prompt.text,
                    max_tokens=self._max_output_tokens,
                )
            )
            decision = parse_decision(raw)
            self._handler.handle(decision, prompt)
            duration = self._clock() - start
            self._ticks_run += 1
            self._last_decision = decision
            self._last_tick_at = datetime.now(UTC).isoformat()
            self._last_duration_seconds = duration
            logger.info(
                "heart tick completed",
                extra={
                    "tick": self._ticks_run,
                    "decision": decision.value,
                    "duration_seconds": duration,
                    "prompt_tokens": prompt.tokens,
                    "items_included": prompt.items_included,
                    "items_dropped": prompt.items_dropped,
                },
            )
            return TickOutcome(
                status="ok", decision=decision, duration_seconds=duration
            )
        finally:
            self._in_flight = False


def build_tick_loop(settings: Settings, heart: HeartRuntime) -> TickLoop:
    """The default `tick_loop_factory` for `create_app` — wires a `TickLoop` to
    `settings.heart.tick`. Ships with no snapshot sources and the default
    `LoggingDecisionHandler`; see `heart.tick.snapshot`'s module docstring for why.
    """
    tick_settings = settings.heart.tick
    return TickLoop(
        heart,
        interval_seconds=tick_settings.interval_seconds,
        jitter_seconds=tick_settings.jitter_seconds,
        max_output_tokens=settings.heart.max_output_tokens,
    )
