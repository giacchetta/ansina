"""`OpencodeInterventionHandler` — the Heart's bounded, deterministic interventions
on the OpenCode executor. See issue #79.

Gated by `[heart.tick] opencode_interventions` (default `false`) — composed by
`build_tick_loop` alongside `JournalDecisionHandler`/`BrainEscalationHandler`/
`FaultInjector`, the same verified-no-op discipline issue #64 established: with
the flag off, `build_tick_loop` never constructs this handler at all, leaving the
exact pre-#79 object graph.

`handle()` is a no-op for every decision but `TickDecision.ACT` — the Heart's own
model output is consumed only as "act / don't act," per the issue's own line.
*Which* action fires (if any) is chosen entirely by `DeterministicInterventionPolicy`
from real, already-reported `OpencodeStateProbe` signals plus real, already-persisted
run ids — never from the model's own text. `OpencodeAction.ESCALATE_TO_BRAIN` is the
one action this handler dispatches itself (reusing `BrainEscalationHandler`'s own
`BrainProvider.stream()` call/journal-column pattern, gated additionally by
`[heart.tick] escalate_to_brain` — the Brain is never reachable through this new
path while that older flag stays off); every other action goes through the probe's
own `apply_intervention(run_id, action)` — the one call site issue #79's own
structural safety test targets, whose only inputs are an `OpencodeAction` member and
an already-persisted run id the *policy* selected.

Every attempt — success, decline, or error, for any action — is journaled through
`HeartJournalRepository.append()`'s now-widened signature, appending a *second* row
sharing the triggering tick's own `tick_number` and `decision=TickDecision.ACT`,
never mutating the first (the same `BrainEscalationHandler`/`JournalDecisionHandler`
shape). A tick where the policy found nothing actionable writes no second row at
all — the common case, mirroring `BrainEscalationHandler`'s own no-op shape for
`idle`/`act`-with-nothing-to-escalate.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

import anyio.to_thread

from ansina.auth.clock import Clock, iso, parse_iso, utc_now
from ansina.brain.events import BrainDone, BrainErrorEvent
from ansina.brain.provider import BrainMessage, BrainProvider, BrainRequest
from ansina.heart.journal import (
    BrainEscalationStatus,
    HeartJournalRepository,
    OpencodeInterventionStatus,
)
from ansina.heart.tick.decision import TickDecision
from ansina.heart.tick.snapshot import TickPrompt
from ansina.heart.tick.sources.opencode_state import OpencodeStateProbe
from ansina.logging import get_logger
from ansina.opencode.interventions import OpencodeAction
from ansina.opencode.repository import OpencodeRun

logger = get_logger(__name__)

# Mirrors `brain_escalation_handler._MAX_DETAIL_LENGTH`'s own reasoning: a reason/
# detail string must still never grow a journal row without limit.
_MAX_DETAIL_LENGTH = 500

# Issue #79 names three budgets as config keys (per-run retries, cooldown ticks,
# per-hour cap); the specific "how many recent failures trigger pause_intake"
# threshold is not one of the four named keys, so it is a plain module constant
# here rather than a fifth config surface — a deliberate, documented scope
# narrowing, not an oversight.
_PAUSE_INTAKE_FAILURE_THRESHOLD = 3

_DECLINED_ESCALATE_FLAG_OFF = "declined: [heart.tick] escalate_to_brain is false"
_DECLINED_NO_BRAIN = "declined: [brain] is not enabled or configured"


class OpencodeInterventionProbe(OpencodeStateProbe, Protocol):
    """`OpencodeStateProbe` (issue #78) plus the one more read
    `DeterministicInterventionPolicy` needs that the observe-only source never
    did: concrete, already-persisted retry candidates, not just a count.
    """

    def recent_retryable_runs(self, *, since: str) -> Sequence[OpencodeRun]:
        """`failed`/`timeout` runs created after `since`, newest first."""
        ...


class OpencodeInterventionTarget(OpencodeInterventionProbe, Protocol):
    """The fuller surface `OpencodeInterventionHandler` depends on — read access
    (above) plus the one mutating seam every non-`ESCALATE_TO_BRAIN` action goes
    through. Satisfied structurally by `ansina.opencode.service.OpencodeRunService`
    with no import in either direction — the same duck-typing precedent
    `OpencodeStateProbe` itself already established, widened by inheritance the
    way `TickController(TickLifecycle, Protocol)` already is.
    """

    async def apply_intervention(
        self, run_id: str | None, action: OpencodeAction
    ) -> tuple[str, str]:
        """Returns `(status, detail)` — `status` one of `"applied"`/`"declined"`/
        `"unsupported"`/`"error"`, the exact values of `OpencodeInterventionStatus`.
        Never called with `OpencodeAction.ESCALATE_TO_BRAIN` — see module
        docstring. `run_id` is `None` only for `OpencodeAction.PAUSE_INTAKE` when
        the policy found no single representative run.
        """
        ...

    def record_intervention_telemetry(
        self,
        run_id: str | None,
        action: OpencodeAction,
        outcome: str,
        *,
        detail: str,
        session_id: str | None = None,
    ) -> None:
        """The one telemetry seam `ESCALATE_TO_BRAIN` uses directly, since that
        action never reaches `apply_intervention` (which emits its own telemetry
        line internally for the other four)."""
        ...


class TickCounter(Protocol):
    """The one thing the cooldown budget needs from `TickLoop` — structural,
    mirroring `heart.tick.fault_injection.TickCounter`'s own "never import
    `loop.py`" discipline.
    """

    @property
    def ticks_run(self) -> int: ...


@dataclass(frozen=True, slots=True)
class InterventionDecision:
    """What `InterventionPolicy.decide()` chose: an `action`, the run it targets
    (`None` for `OpencodeAction.PAUSE_INTAKE`, which has no single triggering run,
    and optionally for `ESCALATE_TO_BRAIN` when no single run is at fault), and a
    short, code-generated `reason` — never model text, journaled verbatim
    (truncated) as `opencode_detail`/the telemetry line's own `detail`.
    """

    action: OpencodeAction
    run_id: str | None
    reason: str


class InterventionPolicy(Protocol):
    """The pluggable decision-maker behind `OpencodeInterventionHandler` —
    structural, like every other policy/port in this codebase. Issue #79 ships
    one implementation, `DeterministicInterventionPolicy`; a later, bench-gated
    issue's model-chosen policy (#15, "Heart as structured extractor") is the
    named path to a second one.
    """

    def decide(
        self, probe: OpencodeInterventionProbe, *, now: datetime
    ) -> InterventionDecision | None: ...


@dataclass
class DeterministicInterventionPolicy:
    """A documented priority ladder, highest rung first, first match wins:

    1. A recent `failed`/`timeout` run with retries remaining
       (`intervention_count < max_retries_per_run`) -> `RETRY`.
    2. A stalled `running` run (the same `last_event_at`-or-`started_at`-vs-
       `stall_seconds` reasoning `OpencodeStateSource` already uses) -> `NUDGE`.
    3. Recent failures/timeouts at or past `_PAUSE_INTAKE_FAILURE_THRESHOLD`
       within `recent_window_minutes` -> `PAUSE_INTAKE` (`run_id=None`).
    4. A real fault that rungs 1-3 did not resolve — whether because a
       short-term cooldown blocked them, or because a candidate's own
       per-run retry budget is exhausted — -> `ESCALATE_TO_BRAIN`. This is
       "no remaining bounded action," not "nothing is wrong": it never fires
       on an empty, fault-free tick.

    `OpencodeAction.COMPACT` is deliberately never selected here (though
    `OpencodeRunService.apply_intervention()` still has a real, verified
    `UNSUPPORTED` branch for it): no existing `OpencodeStateProbe` signal reports
    context-pressure today, and inventing a heuristic ahead of a real signal
    would be exactly the kind of unverified assumption this codebase's "verify,
    don't assume" discipline (`ansina.opencode.events`'s own intentionally-empty
    `RunEventType` lookup table) already refuses elsewhere — deferred to #15's own
    model-chosen-policy path, which might pick a real signal for this rung.

    **Budgets** — two independent gates, deliberately not one, so a persisting
    fault is never simply ignored forever once the short-term gate engages:

    - `cooldown_ticks` gates rungs 1-3 only: no two of `RETRY`/`NUDGE`/
      `PAUSE_INTAKE` within this many ticks of each other (`tick_counter
      .ticks_run`-based, never wall-clock) — this is what makes rung 4 reachable
      in the first place when a fault outlives this short-term spacing.
    - `max_interventions_per_hour` gates *every* rung, rung 4 included — a hard,
      rolling, process-lifetime-only ceiling (wall-clock via the injectable
      `clock`) across the total intervention count, so escalate-as-a-fallback
      cannot itself become the feedback loop this budget exists to prevent.
    - `max_retries_per_run` is the one per-run (not per-tick) budget: read from
      the candidate's own `OpencodeRun.intervention_count`.

    Every `decide()` call that returns a non-`None` decision records that attempt
    against both budgets immediately — before the handler has even tried to apply
    it — since the budgets exist to bound *attempts*, not only successes.

    `bind()` must be called (with the owning `TickLoop`) before `decide()` — see
    that method's own docstring for why this instance can't simply take the loop
    as a constructor argument.
    """

    max_retries_per_run: int = 2
    cooldown_ticks: int = 5
    max_interventions_per_hour: int = 6
    stall_seconds: float = 300.0
    recent_window_minutes: int = 15
    clock: Callable[[], float] = time.monotonic
    _tick_counter: TickCounter | None = field(default=None, init=False)
    _last_intervention_tick: int | None = field(default=None, init=False)
    _recent_intervention_times: list[float] = field(default_factory=list, init=False)

    def bind(self, tick_counter: TickCounter) -> None:
        """Supplies the `TickLoop` this policy's cooldown budget counts ticks
        against — called once, after the owning `TickLoop` exists, the identical
        chicken-and-egg fix `heart.tick.fault_injection.FaultInjector.bind` already
        uses: this policy is built *before* `TickLoop.__init__` (composed into its
        `decision_handler`), so it cannot take the loop itself as a constructor
        argument.
        """
        self._tick_counter = tick_counter

    def _ticks_run(self) -> int:
        if self._tick_counter is None:
            raise RuntimeError(
                "DeterministicInterventionPolicy.bind() must be called before use"
            )
        return self._tick_counter.ticks_run

    def decide(
        self, probe: OpencodeInterventionProbe, *, now: datetime
    ) -> InterventionDecision | None:
        hourly_ok = not self._hourly_budget_exhausted()
        cooldown_ok = hourly_ok and not self._in_cooldown()

        since = iso(now - timedelta(minutes=self.recent_window_minutes))
        retry_candidates = probe.recent_retryable_runs(since=since)
        retryable = [
            run
            for run in retry_candidates
            if run.intervention_count < self.max_retries_per_run
        ]
        if cooldown_ok and retryable:
            run_id = retryable[0].id
            return self._decided(
                OpencodeAction.RETRY,
                run_id,
                reason=f"run {run_id} {retryable[0].status}, retrying",
            )

        stalled_id = self._first_stalled_run_id(probe, now)
        if cooldown_ok and stalled_id is not None:
            return self._decided(
                OpencodeAction.NUDGE,
                stalled_id,
                reason=f"run {stalled_id} stalled, nudging",
            )

        counts = probe.recent_outcome_counts(since=since)
        failure_total = counts.failed + counts.timeout
        if cooldown_ok and failure_total >= _PAUSE_INTAKE_FAILURE_THRESHOLD:
            return self._decided(
                OpencodeAction.PAUSE_INTAKE,
                None,
                reason=(
                    f"{failure_total} opencode failures/timeouts in the last "
                    f"{self.recent_window_minutes}m, pausing intake"
                ),
            )

        # Rung 4: a real fault exists (a retry/stall candidate, regardless of
        # whether its own budget allowed rungs 1-2 to actually fire) and nothing
        # above resolved it — gated by the hourly ceiling only, deliberately not
        # by `cooldown_ticks`: the whole reason rung 4 is reachable at all is
        # that a short-term cooldown, or an exhausted per-run retry budget, can
        # block rungs 1-3 while the fault itself persists.
        candidate_run_id = retry_candidates[0].id if retry_candidates else stalled_id
        if hourly_ok and (
            candidate_run_id is not None
            or failure_total >= _PAUSE_INTAKE_FAILURE_THRESHOLD
        ):
            return self._decided(
                OpencodeAction.ESCALATE_TO_BRAIN,
                candidate_run_id,
                reason="opencode fault persists with no remaining bounded action",
            )
        return None

    def _decided(
        self, action: OpencodeAction, run_id: str | None, *, reason: str
    ) -> InterventionDecision:
        self._last_intervention_tick = self._ticks_run()
        self._recent_intervention_times.append(self.clock())
        return InterventionDecision(action=action, run_id=run_id, reason=reason)

    def _in_cooldown(self) -> bool:
        if self._last_intervention_tick is None:
            return False
        return (self._ticks_run() - self._last_intervention_tick) < self.cooldown_ticks

    def _hourly_budget_exhausted(self) -> bool:
        cutoff = self.clock() - 3600.0
        self._recent_intervention_times = [
            t for t in self._recent_intervention_times if t >= cutoff
        ]
        return len(self._recent_intervention_times) >= self.max_interventions_per_hour

    def _first_stalled_run_id(
        self, probe: OpencodeInterventionProbe, now: datetime
    ) -> str | None:
        threshold = now.timestamp() - self.stall_seconds
        for run in probe.running_runs():
            reference = run.last_event_at or run.started_at
            if reference is None:
                continue
            if parse_iso(reference).timestamp() < threshold:
                return run.id
        return None


class OpencodeInterventionHandler:
    """The `DecisionHandler` (issue #79). See module docstring."""

    def __init__(
        self,
        probe: OpencodeInterventionTarget,
        policy: InterventionPolicy,
        repository: HeartJournalRepository,
        *,
        brain: BrainProvider | None,
        escalate_to_brain_enabled: bool,
        model: str,
        max_output_tokens: int,
        max_entries: int,
        retention_days: int,
        clock: Clock = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._probe = probe
        self._policy = policy
        self._repository = repository
        self._brain = brain
        self._escalate_to_brain_enabled = escalate_to_brain_enabled
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._max_entries = max_entries
        self._retention_days = retention_days
        self._clock = clock
        self._monotonic = monotonic

    async def handle(
        self,
        decision: TickDecision,
        prompt: TickPrompt,
        *,
        tick_number: int,
        duration_seconds: float,
    ) -> None:
        if decision is not TickDecision.ACT:
            return

        now = self._clock()
        # Offloaded: `DeterministicInterventionPolicy.decide()` calls the probe's
        # own blocking sqlite reads (`recent_retryable_runs`/`running_runs`/
        # `recent_outcome_counts`) synchronously — the same reasoning
        # `TickLoop.tick_once()` already offloads `collect_items` for.
        result = await anyio.to_thread.run_sync(
            functools.partial(self._policy.decide, self._probe, now=now)
        )
        if result is None:
            return

        if result.action is OpencodeAction.ESCALATE_TO_BRAIN:
            await self._escalate_to_brain(result, prompt, tick_number=tick_number)
            return

        # Every action but `ESCALATE_TO_BRAIN`/`PAUSE_INTAKE` always names a run;
        # `PAUSE_INTAKE` may legitimately carry `run_id=None` — `apply_intervention`
        # itself handles that case (see its own docstring), so no assertion here.
        status, detail = await self._probe.apply_intervention(
            result.run_id, result.action
        )
        await self._journal(
            tick_number,
            prompt,
            run_id=result.run_id,
            action=result.action,
            outcome=OpencodeInterventionStatus(status),
            detail=detail[:_MAX_DETAIL_LENGTH],
        )

    async def _escalate_to_brain(
        self, result: InterventionDecision, prompt: TickPrompt, *, tick_number: int
    ) -> None:
        if not self._escalate_to_brain_enabled:
            await self._journal_escalate(
                result,
                prompt,
                tick_number=tick_number,
                opencode_outcome=OpencodeInterventionStatus.DECLINED,
                opencode_detail=_DECLINED_ESCALATE_FLAG_OFF,
                brain_status=BrainEscalationStatus.DECLINED,
                brain_detail=_DECLINED_ESCALATE_FLAG_OFF,
                brain_prompt_tokens=None,
                brain_completion_tokens=None,
                call_duration=0.0,
            )
            return
        if self._brain is None:
            await self._journal_escalate(
                result,
                prompt,
                tick_number=tick_number,
                opencode_outcome=OpencodeInterventionStatus.DECLINED,
                opencode_detail=_DECLINED_NO_BRAIN,
                brain_status=BrainEscalationStatus.DECLINED,
                brain_detail=_DECLINED_NO_BRAIN,
                brain_prompt_tokens=None,
                brain_completion_tokens=None,
                call_duration=0.0,
            )
            return

        request = BrainRequest(
            messages=(BrainMessage(role="user", content=prompt.text),),
            model=self._model,
            max_output_tokens=self._max_output_tokens,
        )
        brain_status = BrainEscalationStatus.CALLED
        brain_detail = ""
        brain_prompt_tokens: int | None = None
        brain_completion_tokens: int | None = None
        start = self._monotonic()
        gen = self._brain.stream(request)
        try:
            # `asyncio.CancelledError` is deliberately not caught — see
            # `brain_escalation_handler`'s own module docstring for the identical
            # reasoning: it is the caller's own signal, not a Brain failure, and
            # propagates with no journal row written for this attempt.
            async for event in gen:
                if isinstance(event, BrainDone):
                    if event.usage is not None:
                        brain_prompt_tokens = event.usage.prompt_tokens
                        brain_completion_tokens = event.usage.completion_tokens
                elif isinstance(event, BrainErrorEvent):
                    brain_status = BrainEscalationStatus.ERROR
                    brain_detail = event.message[:_MAX_DETAIL_LENGTH]
                    if event.usage is not None:
                        brain_prompt_tokens = event.usage.prompt_tokens
                        brain_completion_tokens = event.usage.completion_tokens
        finally:
            await gen.aclose()
        call_duration = self._monotonic() - start

        opencode_outcome = (
            OpencodeInterventionStatus.ERROR
            if brain_status is BrainEscalationStatus.ERROR
            else OpencodeInterventionStatus.APPLIED
        )
        await self._journal_escalate(
            result,
            prompt,
            tick_number=tick_number,
            opencode_outcome=opencode_outcome,
            opencode_detail=result.reason[:_MAX_DETAIL_LENGTH],
            brain_status=brain_status,
            brain_detail=brain_detail,
            brain_prompt_tokens=brain_prompt_tokens,
            brain_completion_tokens=brain_completion_tokens,
            call_duration=call_duration,
        )

    async def _journal_escalate(
        self,
        result: InterventionDecision,
        prompt: TickPrompt,
        *,
        tick_number: int,
        opencode_outcome: OpencodeInterventionStatus,
        opencode_detail: str,
        brain_status: BrainEscalationStatus,
        brain_detail: str,
        brain_prompt_tokens: int | None,
        brain_completion_tokens: int | None,
        call_duration: float,
    ) -> None:
        """One shared second row for `OpencodeAction.ESCALATE_TO_BRAIN`, carrying
        both this issue's `opencode_*` columns and issue #64's existing `brain_*`
        columns — reusing the existing Brain-call journaling rather than a second
        one, per the issue's own line.
        """
        self._probe.record_intervention_telemetry(
            result.run_id,
            result.action,
            opencode_outcome.value,
            detail=opencode_detail,
        )
        await anyio.to_thread.run_sync(
            functools.partial(
                self._repository.append,
                created_at=iso(self._clock()),
                tick_number=tick_number,
                decision=TickDecision.ACT,
                note="",
                prompt_tokens=prompt.tokens,
                duration_seconds=call_duration,
                max_entries=self._max_entries,
                retention_days=self._retention_days,
                brain_status=brain_status,
                brain_detail=brain_detail,
                brain_prompt_tokens=brain_prompt_tokens,
                brain_completion_tokens=brain_completion_tokens,
                opencode_run_id=result.run_id,
                opencode_action=result.action,
                opencode_outcome=opencode_outcome,
                opencode_detail=opencode_detail,
            )
        )

    async def _journal(
        self,
        tick_number: int,
        prompt: TickPrompt,
        *,
        run_id: str | None,
        action: OpencodeAction,
        outcome: OpencodeInterventionStatus,
        detail: str,
    ) -> None:
        await anyio.to_thread.run_sync(
            functools.partial(
                self._repository.append,
                created_at=iso(self._clock()),
                tick_number=tick_number,
                decision=TickDecision.ACT,
                note="",
                prompt_tokens=prompt.tokens,
                duration_seconds=0.0,
                max_entries=self._max_entries,
                retention_days=self._retention_days,
                opencode_run_id=run_id,
                opencode_action=action,
                opencode_outcome=outcome,
                opencode_detail=detail,
            )
        )
