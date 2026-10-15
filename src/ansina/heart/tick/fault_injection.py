"""`FaultInjector` — forces real, scheduled daemon-state faults during a soak run so
the resulting act/escalate decisions have ground-truth labels to score against. See
issue #72.

Every soak run and every telemetry day published to the bucket before this issue was
100% `idle` (issue #60's own 8-hour soak; continuous live telemetry since issue #62) —
by construction, not luck: nothing in the soak harness or the live deployment profile
ever makes a real fault happen, so there is no non-idle ground truth anywhere in the
live corpus. `heart.eval.data.tick_fixtures.jsonl`'s `self_state_fault`-tagged rows are
the only non-idle evidence that exists, and they're a fixture snapshot — never
exercised against the real tick cadence on real hardware.

This module deliberately never fabricates snapshot text — the one discipline every
other `StateSnapshotSource`/`DecisionHandler` in this codebase already holds (see
`journal_handler.py`'s own "note is code-generated from real snapshot items, never
invented" reasoning). Instead it forces the exact three real signals
`heart.tick.sources.daemon_state.DaemonStateSource` already reports faithfully, each
played by one role on the shared `FaultInjector`:

- A registered `Readiness` check (`readiness_ok`), flipped `False` for a scheduled
  `READINESS_CHECK` window — the same mechanism a real `"heart"`/`"heart_tick"`/
  `"database"` check already uses; `DaemonStateSource._readiness_item()` reports it
  exactly the way it reports a real one, because as far as that source can tell, it
  *is* one.
- A deliberately raised `FaultInjectionError` from the `DecisionHandler.handle()` role
  for a scheduled `TICK_FAILURE` window — propagates out of `TickLoop.tick_once()` into
  `run()`'s own swallow-and-log handler, which is what actually bumps
  `consecutive_failures`/`failures_total` for real. `build_tick_loop` composes this
  role *last* in `CompositeDecisionHandler`, so the tick's own journal row (and any
  Brain escalation row) is always written first — the injected failure is the tick's
  outcome, not evidence it never happened.
- A real `time.sleep()` from the `StateSnapshotSource.collect()` role for a scheduled
  `TICK_OVERRUN` window — a genuinely slow tick, measured for real by
  `TickLoop._record_success`'s own duration check against `overrun_ratio`, not a
  fabricated "slow" claim.

`[heart.tick.fault_injection] enabled = false` (the default, `config.settings
.FaultInjectionSettings`) is a byte-for-byte no-op — `build_tick_loop` never constructs
a `FaultInjector`, leaving the exact object graph every pre-#72 caller already builds.

A schedule is a flat JSON array of windows (`load_fault_schedule`), each a contiguous
run of tick numbers during which one `FaultKind` is forced. Windows of *different*
kinds may overlap on purpose — that's how a schedule recreates
`tick_fixtures.jsonl`'s own "two simultaneous axes" scenarios (e.g. `self-esc-01`/
`self-esc-03`) against real hardware; two windows of the *same* kind overlapping is
refused as ambiguous. The schedule, plus whatever `GET /heart/journal` actually
recorded for the same tick numbers, is what lets a reader score decision correctness
after the run — see `scripts/heart_soak_report.py`'s fault-injection section.

Enabling this without `[heart.tick] auto_pause_enabled = false` risks a
`tick_failure`/`tick_overrun` window whose `duration_ticks` reaches `[heart.tick]
max_consecutive_failures`, tripping the circuit breaker mid-run and pausing every
window scheduled after it — `build_tick_loop` logs a warning for a schedule shaped that
way, but does not refuse it, since whether that's wanted (proving the breaker itself
trips correctly) or not is the schedule author's call.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any, ClassVar, Protocol

from ansina.errors import HeartError
from ansina.heart.tick.decision import TickDecision
from ansina.heart.tick.snapshot import SnapshotItem, TickPrompt
from ansina.logging import get_logger

logger = get_logger(__name__)


class FaultScheduleError(HeartError):
    """A fault-injection schedule file failed to parse or validate."""

    code: ClassVar[str] = "ansina.heart.fault_schedule_invalid"


class FaultInjectionError(HeartError):
    """Deliberately raised by `FaultInjector.handle()` for a `TICK_FAILURE` window —
    see the module docstring. Never meant to reach anything outside
    `TickLoop.run()`'s own swallow-and-log handler.
    """

    code: ClassVar[str] = "ansina.heart.fault_injected"


class FaultKind(StrEnum):
    """The three real signals `DaemonStateSource` can report — nothing else is a
    valid fault to schedule, since nothing else is something the Heart's real prompt
    would ever genuinely say.
    """

    READINESS_CHECK = "readiness_check"
    TICK_FAILURE = "tick_failure"
    TICK_OVERRUN = "tick_overrun"


@dataclass(frozen=True, slots=True)
class FaultWindow:
    """One scheduled fault — a contiguous run of ticks, by 1-based tick number
    (`TickLoop.tick_once()`'s own `tick_number`, the ordinal `JournalDecisionHandler`
    already persists), during which `FaultInjector` forces `fault` to be genuinely
    true and a correct Heart is expected to reach `expected_decision`.

    `sleep_seconds` is only meaningful for `FaultKind.TICK_OVERRUN` — how long
    `collect()` blocks, chosen by the schedule's own author against their own
    `[heart.tick] interval_seconds`/`overrun_ratio` (this module has no way to see
    either, by design — it only forces real, measured time to pass).
    """

    id: str
    fault: FaultKind
    at_tick: int
    duration_ticks: int
    expected_decision: TickDecision
    note: str = ""
    sleep_seconds: float = 0.0

    @property
    def end_tick(self) -> int:
        """The window's own last covered tick number, inclusive."""
        return self.at_tick + self.duration_ticks - 1

    def covers(self, tick_number: int) -> bool:
        return self.at_tick <= tick_number <= self.end_tick


def load_fault_schedule(path: Path | str) -> tuple[FaultWindow, ...]:
    """Parse and validate a fault-injection schedule — a JSON array of objects, each
    matching `FaultWindow`'s own fields. Raises `FaultScheduleError` on anything
    malformed, the same "fail loudly before uvicorn binds a port" treatment
    `OidcSettings`'s own validators give a config that could never actually work.

    Returned sorted by `at_tick` — not load-bearing for `FaultInjector` itself (which
    scans the whole schedule every call), but makes a rendered/logged schedule read in
    the order a human expects.
    """
    resolved = Path(path)
    try:
        raw_text = resolved.read_text("utf-8")
    except OSError as exc:
        raise FaultScheduleError(f"{resolved}: could not be read ({exc})") from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise FaultScheduleError(f"{resolved}: invalid JSON ({exc})") from exc
    if not isinstance(raw, list):
        raise FaultScheduleError(f"{resolved}: must be a JSON array of windows")

    windows = [
        _parse_window(entry, index=i, source=str(resolved))
        for i, entry in enumerate(raw)
    ]
    _validate_no_duplicate_ids(windows, source=str(resolved))
    _validate_no_same_kind_overlap(windows, source=str(resolved))
    return tuple(sorted(windows, key=lambda window: window.at_tick))


def _parse_window(entry: Any, *, index: int, source: str) -> FaultWindow:  # noqa: ANN401
    if not isinstance(entry, dict):
        raise FaultScheduleError(f"{source}[{index}]: must be a JSON object")
    try:
        window_id = str(entry["id"])
        fault = FaultKind(entry["fault"])
        at_tick = int(entry["at_tick"])
        duration_ticks = int(entry["duration_ticks"])
        expected_decision = TickDecision(entry["expected_decision"])
    except KeyError as exc:
        raise FaultScheduleError(f"{source}[{index}]: missing field {exc}") from exc
    except ValueError as exc:
        raise FaultScheduleError(f"{source}[{index}]: {exc}") from exc
    note = str(entry.get("note", ""))
    sleep_seconds = float(entry.get("sleep_seconds", 0.0))

    if at_tick < 1:
        raise FaultScheduleError(
            f"{source}[{index}] ({window_id}): at_tick must be >= 1"
        )
    if duration_ticks < 1:
        raise FaultScheduleError(
            f"{source}[{index}] ({window_id}): duration_ticks must be >= 1"
        )
    if fault is FaultKind.TICK_OVERRUN and sleep_seconds <= 0:
        raise FaultScheduleError(
            f"{source}[{index}] ({window_id}): sleep_seconds must be > 0 for a "
            "tick_overrun window"
        )
    if fault is not FaultKind.TICK_OVERRUN and sleep_seconds:
        raise FaultScheduleError(
            f"{source}[{index}] ({window_id}): sleep_seconds is only meaningful for "
            "a tick_overrun window"
        )
    return FaultWindow(
        id=window_id,
        fault=fault,
        at_tick=at_tick,
        duration_ticks=duration_ticks,
        expected_decision=expected_decision,
        note=note,
        sleep_seconds=sleep_seconds,
    )


def _validate_no_duplicate_ids(windows: Sequence[FaultWindow], *, source: str) -> None:
    seen: set[str] = set()
    for window in windows:
        if window.id in seen:
            raise FaultScheduleError(f"{source}: duplicate window id {window.id!r}")
        seen.add(window.id)


def _validate_no_same_kind_overlap(
    windows: Sequence[FaultWindow], *, source: str
) -> None:
    """Two windows of the *same* `FaultKind` covering the same tick is ambiguous —
    which one's `note`/`expected_decision` applies? Windows of *different* kinds are
    deliberately allowed to overlap — see the module docstring.
    """
    by_kind: dict[FaultKind, list[FaultWindow]] = {}
    for window in windows:
        by_kind.setdefault(window.fault, []).append(window)
    for fault, group in by_kind.items():
        ordered = sorted(group, key=lambda window: window.at_tick)
        for earlier, later in pairwise(ordered):
            if earlier.end_tick >= later.at_tick:
                raise FaultScheduleError(
                    f"{source}: {fault.value} windows {earlier.id!r} and "
                    f"{later.id!r} overlap at tick {later.at_tick}"
                )


class TickCounter(Protocol):
    """The one thing the `collect()`/`readiness_ok()` roles need from `TickLoop` —
    structural, mirroring `heart.tick.sources.daemon_state.TickStats`'s own "never
    import `loop.py`" discipline.
    """

    @property
    def ticks_run(self) -> int: ...


class FaultInjector:
    """See the module docstring for the three roles this plays. Must be `bind()`-ed
    to the owning `TickLoop` once it exists — it's built *before* `TickLoop.__init__`
    (composed into its `decision_handler`), the same ordering constraint
    `TickLoop.add_source`'s own docstring documents for `DaemonStateSource`.
    """

    name = "fault_injection"

    def __init__(
        self,
        schedule: Sequence[FaultWindow],
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._schedule = tuple(schedule)
        self._sleep = sleep
        self._tick: TickCounter | None = None

    @property
    def schedule(self) -> tuple[FaultWindow, ...]:
        return self._schedule

    def bind(self, tick: TickCounter) -> None:
        """Supplies the tick counter the `collect()`/`readiness_ok()` roles
        approximate the upcoming tick number from. Called once, after the owning
        `TickLoop` exists.
        """
        self._tick = tick

    def _active(self, fault: FaultKind, tick_number: int) -> FaultWindow | None:
        for window in self._schedule:
            if window.fault is fault and window.covers(tick_number):
                return window
        return None

    def _upcoming_tick_number(self) -> int:
        """`ticks_run + 1` — the tick about to run. Approximate for the
        `collect()`/`readiness_ok()` roles (both read ahead of the tick's own final
        number); the `handle()` role below gets the exact number instead, as a
        parameter. A one-tick boundary imprecision here is an accepted, documented
        approximation for a dev-only tool, not a production guarantee.
        """
        if self._tick is None:
            raise RuntimeError("FaultInjector.bind() must be called before use")
        return self._tick.ticks_run + 1

    def collect(self) -> tuple[SnapshotItem, ...]:
        """The `StateSnapshotSource` role: sleeps for real on an active
        `TICK_OVERRUN` window. Contributes no snapshot text of its own — the overrun
        is reported by `DaemonStateSource._faults_item()` once `TickLoop` measures
        it, never fabricated here.
        """
        window = self._active(FaultKind.TICK_OVERRUN, self._upcoming_tick_number())
        if window is not None:
            self._sleep(window.sleep_seconds)
        return ()

    def readiness_ok(self) -> bool:
        """The registered `Readiness` check role: `False` for the duration of an
        active `READINESS_CHECK` window — read fresh on every call, the same
        "live, not cached" contract every other registered check already has.
        """
        active = self._active(FaultKind.READINESS_CHECK, self._upcoming_tick_number())
        return active is None

    async def handle(
        self,
        decision: TickDecision,
        prompt: TickPrompt,
        *,
        tick_number: int,
        duration_seconds: float,
    ) -> None:
        """The `DecisionHandler` role: raises `FaultInjectionError` on an active
        `TICK_FAILURE` window, using `tick_number` exactly rather than approximating
        it — the one role here that gets the real number as a parameter instead of
        reading `TickCounter.ticks_run` ahead of time.
        """
        window = self._active(FaultKind.TICK_FAILURE, tick_number)
        if window is not None:
            logger.warning(
                "heart tick: fault injection forcing a tick failure",
                extra={"tick": tick_number, "window": window.id},
            )
            raise FaultInjectionError(
                f"fault injection window {window.id!r} forces a tick failure at "
                f"tick {tick_number}"
            )
