"""`DaemonStateSource` — the tick loop's first real `StateSnapshotSource`. See issue
#54.

Everything here is a fact the daemon already holds; no new schema, no new subsystem.
The whole point is to give the Heart genuine input instead of the empty snapshot every
tick has rendered since issue #11: process uptime, every `Readiness` check's *live*
value, `Database.is_healthy()`, the last completed tick's own decision/duration, and
the failure counters `TickLoop` now tracks (issue #54's other half, the circuit
breaker).

Deliberately depends on three small structural `Protocol`s (`ReadinessProbe`,
`HealthProbe`, `TickStats`) rather than importing `ansina.api.readiness.Readiness` or
`ansina.storage.Database` concretely — `ansina.api` imports `create_app`, which imports
`ansina.heart`, so a concrete import here would invert that layering. Same
"structural, like `HeartRuntime`" idiom `heart.tick.snapshot.StateSnapshotSource`
itself already uses. This module also never imports from `heart.tick.loop` —
`TickStats` is satisfied by `TickLoop` structurally, keeping `loop.py ->
sources/daemon_state.py` a one-way dependency (`loop.py` constructs a
`DaemonStateSource` via `build_tick_loop`, not the other way around).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Protocol

from ansina.heart.tick.decision import TickDecision
from ansina.heart.tick.snapshot import SnapshotItem

# Ascending priority bands: `build_prompt` trims lowest-priority-first, so anything that
# names a live fault sits in `_PRIORITY_FAULT` to make it the last thing ever dropped.
_PRIORITY_BACKGROUND = 0
_PRIORITY_ACTIVITY = 0
_PRIORITY_NOMINAL = 10
_PRIORITY_FAULT = 100


class ReadinessProbe(Protocol):
    """The one method this source needs from `api.readiness.Readiness` — structural, so
    this module never imports the concrete class.
    """

    def snapshot(self) -> Mapping[str, bool]: ...


class HealthProbe(Protocol):
    """The one method this source needs from `storage.Database` — structural, mirroring
    `ReadinessProbe` above.
    """

    def is_healthy(self) -> bool: ...


class TickStats(Protocol):
    """The subset of `TickLoop`'s own state this source reports back to the Heart —
    structural, so this module never imports `heart.tick.loop`. Satisfied by `TickLoop`
    without either module naming the other.
    """

    @property
    def ticks_run(self) -> int: ...
    @property
    def last_decision(self) -> TickDecision | None: ...
    @property
    def last_duration_seconds(self) -> float | None: ...
    @property
    def failures_total(self) -> int: ...
    @property
    def consecutive_failures(self) -> int: ...
    @property
    def consecutive_overruns(self) -> int: ...


def _format_uptime(seconds: float) -> str:
    """A short, human-readable duration — the Heart reads this as prose, not a number to
    do arithmetic on, so coarse rounding (minutes past the first hour) is deliberate.
    """
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{total_seconds}s"


class DaemonStateSource:
    """A `StateSnapshotSource` over the daemon's own live state.

    Constructed once, inside `build_tick_loop` — `clock()` at construction time is
    therefore process boot (or tick-loop assembly, which happens at the same moment in
    `create_app`), and `collect()`'s uptime item is measured against it on every call.
    No field is ever cached across calls: `readiness.snapshot()` and
    `database.is_healthy()` are read fresh every time, which is what makes this source
    live rather than a point-in-time snapshot of what things looked like at
    construction.
    """

    name = "daemon_state"

    def __init__(
        self,
        tick: TickStats,
        *,
        database: HealthProbe,
        readiness: ReadinessProbe,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tick = tick
        self._database = database
        self._readiness = readiness
        self._clock = clock
        self._started_at = clock()

    def collect(self) -> tuple[SnapshotItem, ...]:
        """Exactly five items, always, in a stable shape — a stable prompt shape is
        what makes `heart.eval`'s hand-labelled fixtures a faithful proxy for
        production text.
        """
        return (
            self._uptime_item(),
            self._readiness_item(),
            self._database_item(),
            self._activity_item(),
            self._faults_item(),
        )

    def _uptime_item(self) -> SnapshotItem:
        uptime = _format_uptime(self._clock() - self._started_at)
        return SnapshotItem(
            source=self.name,
            text=f"Daemon uptime: {uptime}.",
            priority=_PRIORITY_BACKGROUND,
        )

    def _readiness_item(self) -> SnapshotItem:
        """Excludes the `"database"` check specifically: `_database_item` below already
        reports that exact fact (`api/readiness.py` registers `"database"` from the same
        `Database.is_healthy` this source calls directly) — reporting it twice would
        double-count one root cause as two lines of "something's wrong," inflating the
        apparent problem count for a reader (human or Heart) without adding information.
        """
        checks = {
            name: ok
            for name, ok in self._readiness.snapshot().items()
            if name != "database"
        }
        failing = sorted(name for name, ok in checks.items() if not ok)
        if failing:
            return SnapshotItem(
                source=self.name,
                text=(
                    "Readiness check(s) currently failing: " + ", ".join(failing) + "."
                ),
                priority=_PRIORITY_FAULT,
            )
        return SnapshotItem(
            source=self.name,
            text=f"All {len(checks)} readiness check(s) currently passing.",
            priority=_PRIORITY_NOMINAL,
        )

    def _database_item(self) -> SnapshotItem:
        if self._database.is_healthy():
            return SnapshotItem(
                source=self.name,
                text="Database connection is healthy.",
                priority=_PRIORITY_NOMINAL,
            )
        return SnapshotItem(
            source=self.name,
            text="Database connection is currently unhealthy.",
            priority=_PRIORITY_FAULT,
        )

    def _activity_item(self) -> SnapshotItem:
        last_decision = self._tick.last_decision
        last_duration = self._tick.last_duration_seconds
        if last_decision is None:
            text = f"Ticks completed: {self._tick.ticks_run}. No tick has run yet."
        else:
            duration_text = (
                f"{last_duration:.2f}s" if last_duration is not None else "unknown"
            )
            text = (
                f"Ticks completed: {self._tick.ticks_run}. Last decision: "
                f"{last_decision.value} (took {duration_text})."
            )
        return SnapshotItem(source=self.name, text=text, priority=_PRIORITY_ACTIVITY)

    def _faults_item(self) -> SnapshotItem:
        """Priority tracks the *live* fault state (the consecutive counters), never the
        lifetime `failures_total` alone — that total never resets (by design, see
        `TickLoop.failures_total`'s own docstring), so a loop that failed once, years
        ago, and has been healthy ever since must not stay pinned at fault priority for
        the rest of the process's life just because the number is nonzero.
        """
        failures_total = self._tick.failures_total
        consecutive_failures = self._tick.consecutive_failures
        consecutive_overruns = self._tick.consecutive_overruns
        if not (consecutive_failures or consecutive_overruns):
            if failures_total:
                text = (
                    f"No current tick failures or overruns "
                    f"({failures_total} historical failure(s))."
                )
            else:
                text = "No tick failures or overruns recorded."
            return SnapshotItem(source=self.name, text=text, priority=_PRIORITY_NOMINAL)
        # Each clause is only present when its own counter is actually live — a
        # leading "0 consecutive, 0 total" (the pre-issue-#54-followup phrasing) reads
        # as reassuring even when overruns alone are the live problem, burying the one
        # thing that actually needs attention behind two zeros.
        parts: list[str] = []
        if consecutive_failures:
            parts.append(
                f"{consecutive_failures} consecutive tick failure(s) "
                f"({failures_total} total)."
            )
        if consecutive_overruns:
            parts.append(
                f"{consecutive_overruns} consecutive tick(s) ran abnormally slow "
                "(a possible stuck or overloaded backend)."
            )
        return SnapshotItem(
            source=self.name, text=" ".join(parts), priority=_PRIORITY_FAULT
        )
