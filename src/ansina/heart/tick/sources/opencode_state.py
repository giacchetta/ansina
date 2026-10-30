"""`OpencodeStateSource` — the tick loop's third real `StateSnapshotSource`. See
issue #78.

Observe-only, exactly like issue #54's `DaemonStateSource` was before issue #64 gave
the Heart a handler to act with: this module only gives the Heart eyes onto the
OpenCode executor's own live state (queued/running counts, stalled runs, recent
failures/timeouts/permission-denials, whether intake is paused). No decision handler,
no action, no model call — a later, still-pending issue (#79, blocked by this one)
is what the Heart eventually acts through.

Deliberately depends on one small structural `Protocol` (`OpencodeStateProbe`, plus
its two attribute-only sub-`Protocol`s `RunningRunState`/`OutcomeCounts`) rather than
importing `ansina.opencode.service.OpencodeRunService` concretely or
`ansina.api` at all — the same `ReadinessProbe`/`HealthProbe`/`TickStats` discipline
`sources/daemon_state.py` already documents, and the same reason: `ansina.api`
imports `create_app`, which imports `ansina.heart`, so a concrete import here would
invert that layering. `OpencodeRunService` satisfies this Protocol structurally
without importing this module at all (pure duck typing, the identical `TickStats`
precedent) — see its own `RunningRunSnapshot`/`RecentOutcomeCounts` dataclasses.

**Priority bands** — the full ordering across every registered source, since
trimming order (`build_prompt` drops lowest-priority-first) *is* the contract:

    DaemonStateSource fault    = 100   (the daemon's own health always wins)
    OpencodeStateSource fault  =  50   <- this module
    DaemonStateSource nominal  =  10
    OpencodeStateSource info   =   5   <- this module
    DaemonStateSource activity/background = 0
    RecentJournalSource        = -10   (first thing dropped under budget pressure)

This source's own fault band (50) outranks its own info band (5); both sit strictly
below `DaemonStateSource`'s fault band (100, the daemon's own health is always
reported last-dropped) and strictly above `RecentJournalSource` (-10, the first thing
ever trimmed).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Protocol

from ansina.auth.clock import iso, parse_iso, utc_now
from ansina.heart.tick.snapshot import SnapshotItem

_PRIORITY_FAULT = 50
_PRIORITY_INFO = 5


class RunningRunState(Protocol):
    """One `running` run's own activity — attribute-only, structural, satisfied by
    `ansina.opencode.service.RunningRunSnapshot` with no import in either direction.
    `last_event_at` is `None` when no event has arrived for this run yet, in which
    case `started_at` is the staleness reference instead.
    """

    @property
    def id(self) -> str: ...
    @property
    def started_at(self) -> str | None: ...
    @property
    def last_event_at(self) -> str | None: ...


class OutcomeCounts(Protocol):
    """Recent failed/timeout/permission-denied counts — attribute-only, structural,
    satisfied by `ansina.opencode.service.RecentOutcomeCounts` with no import in
    either direction.
    """

    @property
    def failed(self) -> int: ...
    @property
    def timeout(self) -> int: ...
    @property
    def permission_denied(self) -> int: ...


class OpencodeStateProbe(Protocol):
    """The one structural surface this source needs from `OpencodeRunService` —
    satisfied without either module importing the other.
    """

    @property
    def intake_paused(self) -> bool: ...

    def counts_by_status(self) -> Mapping[str, int]:
        """`{"queued": n, "running": n}` — a status missing from the mapping means
        zero current rows in that status, not an error.
        """
        ...

    def running_runs(self) -> Sequence[RunningRunState]:
        """Every currently-`running` run — bounded by
        `[opencode] max_concurrent_runs` (small), never a full-table scan."""
        ...

    def recent_outcome_counts(self, *, since: str) -> OutcomeCounts:
        """Failed/timeout/permission-denied counts for runs created after
        `since` (an ISO 8601 timestamp, compared lexicographically — the same
        caller-supplied-clock discipline every table in this codebase follows)."""
        ...


class OpencodeStateSource:
    """A `StateSnapshotSource` over the OpenCode executor's own live state.

    Constructed once, inside `build_tick_loop`, only when the executor actually
    exists (`[opencode] enabled` + a passed preflight — i.e. `opencode` is not
    `None`). `collect()` reads `probe` fresh on every call, the same "live, never
    cached" discipline `DaemonStateSource` already documents.
    """

    name = "opencode_state"

    def __init__(
        self,
        probe: OpencodeStateProbe,
        *,
        stall_seconds: float,
        recent_window_minutes: int,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._probe = probe
        self._stall_seconds = stall_seconds
        self._recent_window_minutes = recent_window_minutes
        self._clock = clock

    def collect(self) -> tuple[SnapshotItem, ...]:
        """Exactly three items, always — the same "stable prompt shape" reasoning
        `DaemonStateSource.collect()` already documents for its own fixed five.
        """
        now = self._clock()
        return (
            self._activity_item(),
            self._stalled_item(now),
            self._recent_outcomes_item(now),
        )

    def _activity_item(self) -> SnapshotItem:
        counts = self._probe.counts_by_status()
        queued = counts.get("queued", 0)
        running = counts.get("running", 0)
        intake = "paused" if self._probe.intake_paused else "accepting new runs"
        return SnapshotItem(
            source=self.name,
            text=(f"OpenCode: {queued} queued, {running} running. Intake {intake}."),
            priority=_PRIORITY_INFO,
        )

    def _stalled_item(self, now: datetime) -> SnapshotItem:
        """A `running` run is stalled when it's gone `stall_seconds` with no new
        event — measured from `last_event_at`, falling back to `started_at` when no
        event has arrived yet. A run that keeps producing events stays healthy no
        matter how long it's been running — the long-but-healthy case is
        deliberately *not* reported here at all.
        """
        threshold = now.timestamp() - self._stall_seconds
        stalled_ids: list[str] = []
        for run in self._probe.running_runs():
            reference = run.last_event_at or run.started_at
            if reference is None:
                # No event yet and no `started_at` either — can't happen through any
                # designed path (`started_at` is always set before a run reaches
                # `running`), but reporting "not stalled" rather than raising keeps
                # one malformed row from taking down the whole snapshot.
                continue
            if parse_iso(reference).timestamp() < threshold:
                stalled_ids.append(run.id)
        if stalled_ids:
            return SnapshotItem(
                source=self.name,
                text=(
                    f"{len(stalled_ids)} OpenCode run(s) stalled (no activity for "
                    f">= {self._stall_seconds:.0f}s): " + ", ".join(stalled_ids) + "."
                ),
                priority=_PRIORITY_FAULT,
            )
        return SnapshotItem(
            source=self.name, text="No stalled OpenCode runs.", priority=_PRIORITY_INFO
        )

    def _recent_outcomes_item(self, now: datetime) -> SnapshotItem:
        since = iso(now - timedelta(minutes=self._recent_window_minutes))
        counts = self._probe.recent_outcome_counts(since=since)
        if counts.failed or counts.timeout or counts.permission_denied:
            parts = [
                f"{counts.failed} failed",
                f"{counts.timeout} timed out",
                f"{counts.permission_denied} permission denial(s)",
            ]
            return SnapshotItem(
                source=self.name,
                text=(
                    f"OpenCode, last {self._recent_window_minutes}m: "
                    + ", ".join(parts)
                    + "."
                ),
                priority=_PRIORITY_FAULT,
            )
        return SnapshotItem(
            source=self.name,
            text=(
                f"No OpenCode failures, timeouts, or permission denials in the "
                f"last {self._recent_window_minutes}m."
            ),
            priority=_PRIORITY_INFO,
        )
