"""`OpencodeRunService` — owns the queue/semaphore/background tasks that turn a
submitted `RunRequest` into a real `AgentRunner.run()` call, persisting every state
transition to `opencode_runs` and emitting every lifecycle event as telemetry. See
issue #77.

This is a new *kind* of object for this codebase: not a request-scoped repository
call, but a long-lived, stateful owner of concurrent background work — shaped like
`ansina.heart.tick.loop.TickLoop`'s `start()`/`stop()`/`is_healthy()` lifecycle, but
managing N concurrently in-flight per-run `asyncio.Task`s rather than one looped task.

**Telemetry before the route returns, enforced, not assumed**: `submit()` calls
`OpencodeTelemetryWriter.record_submitted()` synchronously, before it returns the
created `OpencodeRun` — never scheduled onto the background task — which is what
makes "every run produces a `submitted` telemetry line before the route returns" a
property this module's own code guarantees rather than an ordering convention the
caller has to get right.

`tool_call_count`/`permission_denied_count` on `opencode_runs` are reserved columns,
always `0` as of this issue: `ansina.opencode.events`'s own classification table
(`_RAW_TYPE_TO_EVENT_TYPE`) is intentionally empty until a real captured
`opencode run --format json` sample exists to populate it from (see that module's own
docstring) — inventing a guessed payload-key heuristic here, ahead of that, would be
exactly the kind of unverified assumption this codebase's "verify, don't assume"
discipline (the Vector-0.45.x pin, the v1-vs-v2 `--version` shape finding) refuses
elsewhere. `event_count` (every classified line) and `parse_error_count`
(`RunOutcome.events_skipped` — lines that failed to parse, already tracked by
`OpencodeCliRunner` itself) need no such heuristic and are populated for real.
`intervention_count` is likewise reserved — populated only once a later Heart
intervention issue actually calls `pause_intake()`/a retry/nudge action against a
specific run.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

import anyio.to_thread

from ansina.auth.clock import iso, utc_now
from ansina.config.settings import OpencodeSettings, Settings
from ansina.errors import (
    OpencodeBusyError,
    OpencodeIntakePausedError,
    OpencodeRunFailedError,
)
from ansina.logging import get_logger
from ansina.opencode.events import RunRequest, RunStatus
from ansina.opencode.interventions import (
    OpencodeAction,
    build_nudge_request,
    build_retry_request,
)
from ansina.opencode.preflight import build_opencode_service
from ansina.opencode.repository import OpencodeRun, OpencodeRunRepository
from ansina.opencode.telemetry import (
    OpencodeTelemetryWriter,
    build_opencode_telemetry_writer,
)

if TYPE_CHECKING:
    from ansina.opencode.events import RunEvent
    from ansina.opencode.runner import AgentRunner, OnEvent
    from ansina.storage.database import Database

# issue #79: runs eligible for `OpencodeAction.RETRY` — a run that never actually
# completed cleanly. `cancelled` is deliberately excluded: a cancellation is an
# operator/shutdown decision, not a failure the Heart should second-guess by
# resubmitting it.
_RETRYABLE_STATUSES = frozenset({"failed", "timeout"})

logger = get_logger(__name__)

Clock = Callable[[], datetime]

# `RunStatus` (the `AgentRunner` port's own terminal shape) -> `opencode_runs.status`
# (this table's own, slightly wider shape — it additionally has `queued`/`running`/
# `cancelled`, none of which `RunStatus` needs to represent since those three are
# never something `AgentRunner.run()` itself returns).
_TERMINAL_STATUS_BY_RUN_STATUS: dict[RunStatus, str] = {
    RunStatus.COMPLETED: "succeeded",
    RunStatus.FAILED: "failed",
    RunStatus.TIMEOUT: "timeout",
}


@dataclass(frozen=True, slots=True)
class RunningRunSnapshot:
    """One `running` run's own activity, for issue #78's `OpencodeStateSource`
    stalled-run detection — `started_at` (persisted, from `opencode_runs`) plus
    `last_event_at` (in-memory only, see `OpencodeRunService._last_event_at`'s own
    docstring; `None` when no event has arrived for this run yet, in which case the
    source falls back to `started_at`). Satisfies
    `heart.tick.sources.opencode_state.RunningRunState` structurally — this module
    never imports that one, the same "duck typing across the heart/opencode
    boundary" shape `TickStats`/`TickLoop` already establish.
    """

    id: str
    started_at: str | None
    last_event_at: str | None


@dataclass(frozen=True, slots=True)
class RecentOutcomeCounts:
    """`failed`/`timeout`/`permission_denied` counts over some recent window —
    satisfies `heart.tick.sources.opencode_state.OutcomeCounts` structurally, the
    same reasoning as `RunningRunSnapshot` above.
    """

    failed: int
    timeout: int
    permission_denied: int


class OpencodeRunServiceFactory(Protocol):
    """The shape of `build_opencode_run_service` (and any test double standing in
    for it) — `create_app`'s `opencode_factory` parameter. A plain `Callable[...]`
    type alias can't express `db` as keyword-only under `mypy --strict`, the same
    `TickLoopFactory` (`ansina.heart.tick.loop`) reasoning — `settings` is marked
    positional-only (`/`) so an implementation's own parameter name for it needn't
    match this Protocol's.
    """

    def __call__(
        self, settings: Settings, /, *, db: Database
    ) -> OpencodeRunService | None: ...


class OpencodeRunService:
    """Submission, execution, cancellation, and the intake kill switch — the one
    object `app.state.opencode` holds.
    """

    def __init__(
        self,
        *,
        runner: AgentRunner,
        repository: OpencodeRunRepository,
        telemetry: OpencodeTelemetryWriter | None,
        settings: OpencodeSettings,
        clock: Clock = utc_now,
    ) -> None:
        self._runner = runner
        self._repository = repository
        self._telemetry = telemetry
        self._settings = settings
        self._clock = clock
        self._intake_paused = settings.intake_paused_on_boot
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # Issue #78: each currently-running run's own last-event timestamp, kept
        # *only* in memory — `opencode_runs` has no `last_event_at` column (that
        # issue's own AC explicitly ships no migration), and persisting a value
        # that's rewritten on every single `RunEvent` would be the wrong trade
        # anyway. Populated by `_execute`'s `on_event` closure, popped on that run's
        # own terminal transition (`_forget_last_event`) so this never grows
        # unbounded — the same per-run cleanup shape `_forget_task` already uses.
        self._last_event_at: dict[str, str] = {}

    @property
    def repository(self) -> OpencodeRunRepository:
        """Read-only — lets a route (`GET /opencode/runs`, `GET /opencode/status`)
        query run state directly without this service growing a pass-through method
        for every repository read.
        """
        return self._repository

    # --- issue #78: the `OpencodeStateProbe` surface ---------------------------
    #
    # All three methods below are deliberately *synchronous*, unlike the rest of
    # this class: `heart.tick.snapshot.StateSnapshotSource.collect()` is a sync
    # Protocol, and `TickLoop.tick_once()` already offloads the *whole* snapshot
    # collection via `anyio.to_thread.run_sync` — so a probe method awaiting here
    # would be awaiting from inside a worker thread with no running event loop.
    # The same reasoning `DaemonStateSource`'s `HealthProbe`/`ReadinessProbe` calls
    # already rely on (`Database.is_healthy`/`Readiness.snapshot` are sync despite
    # living in an async app).

    def counts_by_status(self) -> dict[str, int]:
        return self._repository.counts_by_status()

    def running_runs(self) -> list[RunningRunSnapshot]:
        """Every currently-`running` row, each paired with this service's own
        in-memory `last_event_at` for that run id (`None` if no event has arrived
        yet — `OpencodeStateSource` falls back to `started_at` in that case).
        """
        return [
            RunningRunSnapshot(
                id=run.id,
                started_at=run.started_at,
                last_event_at=self._last_event_at.get(run.id),
            )
            for run in self._repository.list_running()
        ]

    def recent_retryable_runs(self, *, since: str) -> list[OpencodeRun]:
        """Issue #79: the read `DeterministicInterventionPolicy` scans for an
        `OpencodeAction.RETRY` candidate — synchronous, same reasoning as the
        three `OpencodeStateProbe` methods above (called from inside a worker
        thread the caller itself offloaded to, never from the event loop
        directly)."""
        return self._repository.recent_retryable_runs(since)

    def recent_outcome_counts(self, *, since: str) -> RecentOutcomeCounts:
        failed, timeout, permission_denied = self._repository.recent_outcome_counts(
            since
        )
        return RecentOutcomeCounts(
            failed=failed, timeout=timeout, permission_denied=permission_denied
        )

    @property
    def intake_paused(self) -> bool:
        return self._intake_paused

    def pause_intake(self) -> None:
        """The kill switch: `POST /opencode/intake/pause` today, a later issue's
        bounded Heart `pause_intake` intervention tomorrow — both call this exact
        method, per the issue's own "the kill switch ... calls through the same
        service method an operator calls" line. In-flight runs are unaffected; only
        `submit()` (new work) is refused while this is set.
        """
        self._intake_paused = True

    def resume_intake(self) -> None:
        self._intake_paused = False

    def is_healthy(self) -> bool:
        """Always `True` once constructed — unlike `HeartRuntime.is_healthy`/
        `Database.is_healthy`, there is no external dependency left to probe here:
        `build_opencode_service`'s own preflight already proved the binary/workspace/
        config/permission chain before this service was ever handed a runner. Feeds
        `Readiness` (`api/readiness.py`) the same `register("opencode", ...)` shape
        `heart`/`heart_tick` already use.
        """
        return True

    async def submit(
        self, request: RunRequest, *, retry_of: str | None = None
    ) -> OpencodeRun:
        """Validate, persist a `queued` row, emit the `submitted` telemetry line,
        then launch (and track) a background task that actually drives the run.
        Raises `OpencodeIntakePausedError` (409)/`OpencodeRunFailedError` (400, via
        the shared `ansina.request.invalid` code at the route layer)/
        `OpencodeBusyError` (429) — all three *before* any `opencode_runs` row is
        created, so a refusal never leaves a `queued` row sitting there.

        `retry_of` (issue #79) is `None` for every pre-#79 call site (the route) —
        `apply_intervention()`'s own `RETRY`/`NUDGE` branches below are the only
        callers that pass a value, recording which earlier run this one
        resubmits/continues via the already-reserved `opencode_runs.retry_of`
        column (see `0012_opencode_runs.sql`'s own "a later retry action's
        self-reference" comment).
        """
        if self._intake_paused:
            raise OpencodeIntakePausedError(
                "opencode intake is paused — POST /opencode/intake/resume to "
                "accept new runs again"
            )
        if len(request.prompt) > self._settings.max_prompt_chars:
            raise OpencodeRunFailedError(
                f"request.prompt is {len(request.prompt)} chars, exceeding "
                f"opencode.max_prompt_chars={self._settings.max_prompt_chars}"
            )

        active = await anyio.to_thread.run_sync(self._repository.count_active)
        if active >= self._settings.queue_max_depth:
            raise OpencodeBusyError(
                f"opencode.queue_max_depth={self._settings.queue_max_depth} "
                "would be exceeded by admitting this run"
            )

        run = await anyio.to_thread.run_sync(
            functools.partial(
                self._repository.create,
                created_at=iso(self._clock()),
                prompt=request.prompt,
                session_id=request.session_id,
                model=request.model,
                agent=request.agent,
                retry_of=retry_of,
            )
        )
        if self._telemetry is not None:
            self._telemetry.record_submitted(run.id, run.session_id, request)

        task = asyncio.create_task(self._execute(run.id, request))
        self._tasks[run.id] = task
        task.add_done_callback(functools.partial(self._forget_task, run_id=run.id))
        return run

    def _forget_task(self, _task: asyncio.Task[None], *, run_id: str) -> None:
        self._tasks.pop(run_id, None)
        # Issue #78: cleaned up here, not only on `_execute`'s own two "normal"
        # terminal paths — a done-callback fires on *every* way a task ends
        # (`CancelledError`, a clean return, or an exception `_execute` itself
        # doesn't catch), so this is the one place that's guaranteed to run
        # regardless, the same reasoning `self._tasks.pop` above already relies on.
        self._last_event_at.pop(run_id, None)

    async def _update_status(
        self,
        run_id: str,
        status: str,
        *,
        started_at: str | None = None,
        completed_at: str | None = None,
        exit_code: int | None = None,
        error_detail: str | None = None,
        event_count: int | None = None,
        parse_error_count: int | None = None,
        duration_seconds: float | None = None,
    ) -> OpencodeRun:
        """Offloads `OpencodeRunRepository.update_status()` — explicit, typed
        keyword params (rather than `**fields: Any`, which `mypy --strict`'s
        `disallow_any_explicit` refuses) matching the subset of that method's own
        signature `_execute` below ever actually passes.
        """
        return await anyio.to_thread.run_sync(
            functools.partial(
                self._repository.update_status,
                run_id,
                status,
                started_at=started_at,
                completed_at=completed_at,
                exit_code=exit_code,
                error_detail=error_detail,
                event_count=event_count,
                parse_error_count=parse_error_count,
                duration_seconds=duration_seconds,
            )
        )

    async def _execute(self, run_id: str, request: RunRequest) -> None:
        """Drives one run end to end: `running` -> `AgentRunner.run()` -> a terminal
        status. `asyncio.CancelledError` is caught *around the whole body*, not just
        the `run()` call — a cancellation delivered before the `running` transition
        even completes must still record `cancelled`, never leave a row stuck
        `queued` forever.
        """
        start = self._clock()
        counters = {"event_count": 0}
        try:
            await self._update_status(run_id, "running", started_at=iso(start))
            if self._telemetry is not None:
                self._telemetry.record_started(run_id, request.session_id)

            def on_event(event: RunEvent) -> None:
                counters["event_count"] += 1
                # Issue #78: every event, classified or not, counts as activity —
                # `OpencodeStateSource`'s stalled-run detection cares whether the
                # child is still producing *anything*, not whether this codebase has
                # learned to classify it yet (`ansina.opencode.events`'s own
                # intentionally empty lookup table).
                self._last_event_at[run_id] = iso(self._clock())
                if self._telemetry is not None:
                    self._telemetry.record_event(run_id, request.session_id, event)

            on_event_cb: OnEvent = on_event
            outcome = await self._runner.run(request, on_event_cb)
        except asyncio.CancelledError:
            duration = (self._clock() - start).total_seconds()
            if self._telemetry is not None:
                self._telemetry.record_cancelled(
                    run_id, request.session_id, duration_seconds=duration
                )
            await self._update_status(
                run_id,
                "cancelled",
                completed_at=iso(self._clock()),
                duration_seconds=duration,
                event_count=counters["event_count"],
            )
            raise

        if self._telemetry is not None:
            self._telemetry.record_outcome(run_id, request.session_id, outcome)
        await self._update_status(
            run_id,
            _TERMINAL_STATUS_BY_RUN_STATUS[outcome.status],
            completed_at=iso(self._clock()),
            exit_code=outcome.exit_code,
            error_detail=outcome.error or "",
            event_count=counters["event_count"],
            parse_error_count=outcome.events_skipped,
            duration_seconds=outcome.duration_seconds,
        )

    async def cancel(self, run_id: str) -> bool:
        """Cancels the in-flight task for `run_id`, if one exists, and waits for
        `_execute`'s own cancellation handling (above) to finish recording the
        `cancelled` outcome before returning. `False` if no task is currently
        tracked for this id — already terminal, or never existed; the route layer
        distinguishes those two by checking the repository directly.
        """
        task = self._tasks.get(run_id)
        if task is None:
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return True

    async def stop(self) -> None:
        """Lifespan shutdown: cancels every tracked task and waits for them, bounded
        by `[opencode] shutdown_timeout_seconds` — the issue's own AC ("no orphan
        opencode process survives a clean daemon exit"). Each task's own
        `OpencodeCliRunner.run()` is what actually terminates->kills its child on
        cancellation (see that module's own `_finish`); this method's job is only to
        make sure every task is actually asked to do that, and given time to finish.
        """
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            _done, pending = await asyncio.wait(
                tasks, timeout=self._settings.shutdown_timeout_seconds
            )
            if pending:
                logger.warning(
                    "opencode run service: tasks still pending after "
                    "shutdown_timeout_seconds, abandoning them",
                    extra={"pending_count": len(pending)},
                )

    async def apply_intervention(
        self, run_id: str | None, action: OpencodeAction
    ) -> tuple[str, str]:
        """The one seam `heart.tick.opencode_intervention_handler
        .OpencodeInterventionHandler` calls for every `OpencodeAction` except
        `ESCALATE_TO_BRAIN` (which the handler itself dispatches directly against
        a `BrainProvider` — this service has no such dependency and none is
        added for this issue). Signature is deliberately `(run_id: str | None,
        action: OpencodeAction)` and nothing else — issue #79's own structural
        safety test asserts no caller ever passes free-text/model-authored
        content through this method. `run_id` is `None` only for
        `OpencodeAction.PAUSE_INTAKE` when the policy found no single
        representative run — every other action always names a real run.

        Returns `(status, detail)` where `status` is one of `"applied"`/
        `"declined"`/`"unsupported"`/`"error"` — the exact string values of
        `ansina.heart.journal.OpencodeInterventionStatus`, returned as plain
        strings rather than that enum so this module (`ansina.opencode`) never
        imports `ansina.heart` (the same acyclic, heart-imports-down-into-
        opencode direction every other cross-package dependency in this codebase
        already takes — `heart.tick.brain_escalation_handler` -> `ansina.brain`,
        never the reverse). The caller converts via
        `OpencodeInterventionStatus(status)` before journaling.
        """
        if action is OpencodeAction.PAUSE_INTAKE:
            # The one action with no required run lookup — it's a global kill
            # switch, not a per-run mutation. `run_id`, when the policy supplied
            # one (a representative recently-failing run), still gets its own
            # `intervention_count` bumped for bookkeeping; `None` is legitimate
            # and simply skips that bump.
            self.pause_intake()
            session_id: str | None = None
            if run_id is not None:
                await anyio.to_thread.run_sync(
                    self._repository.increment_intervention_count, run_id
                )
                run = await anyio.to_thread.run_sync(self._repository.get, run_id)
                session_id = run.session_id if run is not None else None
            outcome = ("applied", "intake paused")
            if self._telemetry is not None:
                self._telemetry.record_intervention(
                    run_id, session_id, action, outcome[0], detail=outcome[1]
                )
            return outcome

        # Every other action always names a real, already-persisted run.
        assert run_id is not None
        run = await anyio.to_thread.run_sync(self._repository.get, run_id)
        if run is None:
            return ("error", f"run {run_id!r} not found")

        if action is OpencodeAction.RETRY:
            if run.status not in _RETRYABLE_STATUSES:
                return ("declined", f"run is {run.status!r}, not retryable")
            new_run = await self.submit(build_retry_request(run), retry_of=run.id)
            await anyio.to_thread.run_sync(
                self._repository.increment_intervention_count, run_id
            )
            detail = f"resubmitted as {new_run.id}"
            outcome = ("applied", detail)
        elif action is OpencodeAction.NUDGE:
            new_run = await self.submit(build_nudge_request(run), retry_of=run.id)
            await anyio.to_thread.run_sync(
                self._repository.increment_intervention_count, run_id
            )
            detail = f"nudged via new run {new_run.id}"
            outcome = ("applied", detail)
        elif action is OpencodeAction.COMPACT:
            # Verified against a real, installed OpenCode v2.1.4 binary at
            # implementation time (see `ansina.opencode.interventions`'s own
            # module docstring for the full finding): a real compact entry
            # point does exist — `POST /api/session/{id}/compact` on the HTTP
            # API `opencode serve` exposes — but there is no CLI subcommand or
            # flag for it at all (confirmed against `opencode --help` and every
            # subcommand's own `--help`), so it is unreachable from the
            # one-shot `opencode run` subprocess model `OpencodeCliRunner`
            # uses. Wiring it would mean a second, server-based adapter
            # (`AgentRunner`'s own module docstring already anticipates
            # exactly this: "a future `opencode serve` + httpx adapter is a
            # pure append") — out of scope for this issue, so this ships a
            # documented `unsupported` no-op rather than a silent gap. An
            # attempted-but-unsupported intervention still counts against the
            # run.
            await anyio.to_thread.run_sync(
                self._repository.increment_intervention_count, run_id
            )
            outcome = (
                "unsupported",
                "opencode has no CLI entry point for compact (only "
                "POST /api/session/{id}/compact over its HTTP API, which "
                "OpencodeCliRunner's one-shot subprocess model cannot reach)",
            )
        else:  # pragma: no cover - ESCALATE_TO_BRAIN never reaches this method
            raise AssertionError(
                f"apply_intervention() does not handle {action!r} — "
                "ESCALATE_TO_BRAIN is dispatched by the handler directly"
            )

        if self._telemetry is not None:
            status, detail = outcome
            self._telemetry.record_intervention(
                run_id, run.session_id, action, status, detail=detail
            )
        return outcome

    def record_intervention_telemetry(
        self,
        run_id: str | None,
        action: OpencodeAction,
        outcome: str,
        *,
        detail: str,
        session_id: str | None = None,
    ) -> None:
        """A thin passthrough to `OpencodeTelemetryWriter.record_intervention` —
        the one seam `OpencodeAction.ESCALATE_TO_BRAIN` uses, since that action is
        dispatched by `heart.tick.opencode_intervention_handler
        .OpencodeInterventionHandler` directly against a `BrainProvider` (this
        service has no such dependency), never through `apply_intervention()`
        above. Keeps "every intervention emits the `intervention` telemetry
        line" true for *every* `OpencodeAction`, including the one this service
        never executes itself. A no-op when telemetry is disabled, the same
        `self._telemetry is not None` guard every other telemetry call site here
        already uses.
        """
        if self._telemetry is not None:
            self._telemetry.record_intervention(
                run_id, session_id, action, outcome, detail=detail
            )


def build_opencode_run_service(
    settings: Settings,
    *,
    db: Database,
    runner_factory: Callable[[Settings], AgentRunner | None] = build_opencode_service,
    telemetry_factory: Callable[
        [Settings], OpencodeTelemetryWriter | None
    ] = build_opencode_telemetry_writer,
) -> OpencodeRunService | None:
    """`None`-when-off, the same shape every other `Opencode*`/`Heart*`/`Brain*`
    factory in this codebase uses — `None` whenever either upstream factory returns
    `None` (in practice, whenever `[opencode] enabled` is `false`, or a failed
    preflight; `Settings._refuse_unobserved_executor` already guarantees
    `[opencode] enabled = true` implies `[telemetry] enabled = true`, so the
    telemetry factory returning `None` while the runner factory doesn't is
    unreachable through a real `Settings` tree — kept as defense in depth rather than
    assumed, the same posture `ansina.opencode.preflight.preflight`'s own redundant
    `telemetry.enabled` check already documents).
    """
    runner = runner_factory(settings)
    if runner is None:
        return None
    telemetry = telemetry_factory(settings)
    if telemetry is None:
        return None
    return OpencodeRunService(
        runner=runner,
        repository=OpencodeRunRepository(db),
        telemetry=telemetry,
        settings=settings.opencode,
    )
