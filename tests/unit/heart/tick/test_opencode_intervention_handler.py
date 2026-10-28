from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from ansina.auth.clock import iso
from ansina.brain.events import BrainDone, BrainErrorClass, BrainErrorEvent, BrainUsage
from ansina.brain.provider import BrainRequest
from ansina.heart.journal import HeartJournalRepository, OpencodeInterventionStatus
from ansina.heart.tick.decision import TickDecision
from ansina.heart.tick.opencode_intervention_handler import (
    DeterministicInterventionPolicy,
    OpencodeInterventionHandler,
)
from ansina.heart.tick.snapshot import SnapshotItem, TickPrompt
from ansina.opencode.interventions import OpencodeAction
from ansina.opencode.repository import OpencodeRun
from ansina.storage.database import Database

_NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)


def _prompt(text: str = "snapshot text") -> TickPrompt:
    return TickPrompt(
        text=text,
        tokens=len(text),
        items_included=1,
        items_dropped=0,
        items=(SnapshotItem(source="opencode_state", text=text, priority=50),),
    )


def _run(
    *,
    run_id: str = "run-1",
    status: str = "failed",
    intervention_count: int = 0,
) -> OpencodeRun:
    return OpencodeRun(
        id=run_id,
        created_at=iso(_NOW - timedelta(minutes=1)),
        started_at=iso(_NOW - timedelta(minutes=2)),
        completed_at=iso(_NOW - timedelta(minutes=1)),
        status=status,
        session_id="session-1",
        prompt="do the thing",
        model=None,
        agent=None,
        exit_code=1,
        error_detail="boom",
        event_count=1,
        tool_call_count=0,
        permission_denied_count=0,
        parse_error_count=0,
        prompt_tokens=None,
        completion_tokens=None,
        duration_seconds=1.0,
        retry_of=None,
        intervention_count=intervention_count,
    )


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


class _FakeTarget:
    """A fake `OpencodeInterventionTarget` — the full read+mutate surface, built
    in the same shape `tests/unit/heart/tick/sources/test_opencode_state.py`'s own
    `_FakeProbe` already uses, widened with `apply_intervention`/
    `recent_retryable_runs`/`record_intervention_telemetry`.
    """

    def __init__(
        self,
        *,
        intake_paused: bool = False,
        counts: Mapping[str, int] | None = None,
        running: Sequence[_FakeRunningRun] = (),
        outcomes: _FakeOutcomeCounts | None = None,
        retryable: Sequence[OpencodeRun] = (),
        apply_result: tuple[str, str] = ("applied", "done"),
    ) -> None:
        self.intake_paused = intake_paused
        self._counts = dict(counts or {})
        self._running = list(running)
        self._outcomes = outcomes if outcomes is not None else _FakeOutcomeCounts()
        self._retryable = list(retryable)
        self._apply_result = apply_result
        self.applied_calls: list[tuple[str | None, OpencodeAction]] = []
        self.telemetry_calls: list[tuple[str | None, OpencodeAction, str, str]] = []

    def counts_by_status(self) -> Mapping[str, int]:
        return dict(self._counts)

    def running_runs(self) -> Sequence[_FakeRunningRun]:
        return list(self._running)

    def recent_outcome_counts(self, *, since: str) -> _FakeOutcomeCounts:
        return self._outcomes

    def recent_retryable_runs(self, *, since: str) -> Sequence[OpencodeRun]:
        return list(self._retryable)

    async def apply_intervention(
        self, run_id: str | None, action: OpencodeAction
    ) -> tuple[str, str]:
        self.applied_calls.append((run_id, action))
        return self._apply_result

    def record_intervention_telemetry(
        self,
        run_id: str | None,
        action: OpencodeAction,
        outcome: str,
        *,
        detail: str,
        session_id: str | None = None,
    ) -> None:
        self.telemetry_calls.append((run_id, action, outcome, detail))


class _FakeTickCounter:
    def __init__(self, ticks_run: int = 10) -> None:
        self.ticks_run = ticks_run


class _FakeBrainProvider:
    """Mirrors `test_brain_escalation_handler.py`'s own fake exactly."""

    def __init__(self, events: Sequence[object]) -> None:
        self._events = list(events)
        self.requests: list[BrainRequest] = []

    def stream(self, request: BrainRequest) -> AsyncGenerator[object]:
        self.requests.append(request)

        async def _gen() -> AsyncGenerator[object]:
            for event in self._events:
                if isinstance(event, BaseException):
                    raise event
                yield event

        return _gen()

    async def aclose(self) -> None:
        pass


def _handler(
    target: _FakeTarget,
    repository: HeartJournalRepository,
    *,
    policy: DeterministicInterventionPolicy | None = None,
    brain: _FakeBrainProvider | None = None,
    escalate_to_brain_enabled: bool = False,
) -> OpencodeInterventionHandler:
    resolved_policy = policy or _policy()
    return OpencodeInterventionHandler(
        target,
        resolved_policy,
        repository,
        brain=brain,  # type: ignore[arg-type]
        escalate_to_brain_enabled=escalate_to_brain_enabled,
        model="test-model",
        max_output_tokens=64,
        max_entries=1000,
        retention_days=365,
        clock=lambda: _NOW,
    )


def _policy(
    *,
    tick_counter: _FakeTickCounter | None = None,
    max_retries_per_run: int = 2,
    cooldown_ticks: int = 5,
    max_interventions_per_hour: int = 6,
    stall_seconds: float = 300.0,
    recent_window_minutes: int = 15,
) -> DeterministicInterventionPolicy:
    policy = DeterministicInterventionPolicy(
        max_retries_per_run=max_retries_per_run,
        cooldown_ticks=cooldown_ticks,
        max_interventions_per_hour=max_interventions_per_hour,
        stall_seconds=stall_seconds,
        recent_window_minutes=recent_window_minutes,
        clock=lambda: _NOW.timestamp(),
    )
    policy.bind(tick_counter or _FakeTickCounter())
    return policy


# --- handle(): only TickDecision.ACT ever reaches the policy -----------------------


async def test_idle_is_a_no_op(db: Database) -> None:
    target = _FakeTarget(retryable=[_run()])
    repository = HeartJournalRepository(db)
    handler = _handler(target, repository)

    await handler.handle(
        TickDecision.IDLE, _prompt(), tick_number=1, duration_seconds=0.1
    )

    assert target.applied_calls == []
    assert repository.list_recent(limit=10) == []


async def test_escalate_decision_is_a_no_op_for_this_handler(db: Database) -> None:
    """`TickDecision.ESCALATE` is `BrainEscalationHandler`'s own domain — this
    handler only ever reacts to `TickDecision.ACT`.
    """
    target = _FakeTarget(retryable=[_run()])
    repository = HeartJournalRepository(db)
    handler = _handler(target, repository)

    await handler.handle(
        TickDecision.ESCALATE, _prompt(), tick_number=1, duration_seconds=0.1
    )

    assert target.applied_calls == []
    assert repository.list_recent(limit=10) == []


async def test_act_with_nothing_actionable_writes_no_row(db: Database) -> None:
    target = _FakeTarget()  # no retryable/stalled/failing runs
    repository = HeartJournalRepository(db)
    handler = _handler(target, repository)

    await handler.handle(
        TickDecision.ACT, _prompt(), tick_number=1, duration_seconds=0.1
    )

    assert target.applied_calls == []
    assert repository.list_recent(limit=10) == []


# --- DeterministicInterventionPolicy: bind() must precede decide() -----------------


def test_decide_before_bind_raises() -> None:
    """The guard only fires once a rung actually tries to read the tick counter
    — an empty, fault-free tick never touches it at all, so this uses a real
    retry candidate to force that path.
    """
    policy = DeterministicInterventionPolicy()
    target = _FakeTarget(retryable=[_run()])

    with pytest.raises(RuntimeError, match="bind"):
        policy.decide(target, now=_NOW)


# --- the priority ladder -------------------------------------------------------------


async def test_rung1_retries_a_failed_run_with_budget_remaining(db: Database) -> None:
    target = _FakeTarget(retryable=[_run(run_id="run-1", intervention_count=0)])
    repository = HeartJournalRepository(db)
    handler = _handler(target, repository)

    await handler.handle(
        TickDecision.ACT, _prompt(), tick_number=1, duration_seconds=0.1
    )

    assert target.applied_calls == [("run-1", OpencodeAction.RETRY)]
    entry = repository.list_recent(limit=1)[0]
    assert entry.opencode_run_id == "run-1"
    assert entry.opencode_action is OpencodeAction.RETRY
    assert entry.opencode_outcome is OpencodeInterventionStatus.APPLIED
    assert entry.decision is TickDecision.ACT


async def test_rung1_skips_a_run_whose_retry_budget_is_exhausted(db: Database) -> None:
    """A run with no retries left is exactly "no remaining bounded action" —
    rung 4 (escalate) is the deliberate fallback rather than silently doing
    nothing forever. With neither Brain gate open, the escalate attempt itself
    is declined, but it is still attempted and journaled.
    """
    target = _FakeTarget(retryable=[_run(run_id="run-1", intervention_count=2)])
    repository = HeartJournalRepository(db)
    handler = _handler(target, repository, policy=_policy(max_retries_per_run=2))

    await handler.handle(
        TickDecision.ACT, _prompt(), tick_number=1, duration_seconds=0.1
    )

    assert target.applied_calls == []  # never dispatched through apply_intervention
    entry = repository.list_recent(limit=1)[0]
    assert entry.opencode_run_id == "run-1"
    assert entry.opencode_action is OpencodeAction.ESCALATE_TO_BRAIN
    assert entry.opencode_outcome is OpencodeInterventionStatus.DECLINED


async def test_rung2_nudges_a_stalled_run_when_no_retry_candidate_exists(
    db: Database,
) -> None:
    stalled = _FakeRunningRun(
        id="run-2",
        started_at=iso(_NOW - timedelta(minutes=10)),
        last_event_at=iso(_NOW - timedelta(minutes=10)),
    )
    target = _FakeTarget(running=[stalled])
    repository = HeartJournalRepository(db)
    handler = _handler(target, repository, policy=_policy(stall_seconds=300.0))

    await handler.handle(
        TickDecision.ACT, _prompt(), tick_number=1, duration_seconds=0.1
    )

    assert target.applied_calls == [("run-2", OpencodeAction.NUDGE)]


async def test_a_running_run_with_no_started_at_or_last_event_at_is_never_stalled(
    db: Database,
) -> None:
    """Defensive — unreachable through any designed path (`started_at` is always
    set before a run reaches `running`), the identical case
    `OpencodeStateSource._stalled_item`'s own test suite already pins.
    """
    malformed = _FakeRunningRun(id="malformed", started_at=None, last_event_at=None)
    target = _FakeTarget(running=[malformed])
    repository = HeartJournalRepository(db)
    handler = _handler(target, repository)

    await handler.handle(
        TickDecision.ACT, _prompt(), tick_number=1, duration_seconds=0.1
    )

    assert target.applied_calls == []
    assert repository.list_recent(limit=10) == []


async def test_rung1_takes_priority_over_rung2(db: Database) -> None:
    retryable = [_run(run_id="retry-me", intervention_count=0)]
    stalled = _FakeRunningRun(
        id="nudge-me",
        started_at=iso(_NOW - timedelta(minutes=10)),
        last_event_at=iso(_NOW - timedelta(minutes=10)),
    )
    target = _FakeTarget(retryable=retryable, running=[stalled])
    repository = HeartJournalRepository(db)
    handler = _handler(target, repository)

    await handler.handle(
        TickDecision.ACT, _prompt(), tick_number=1, duration_seconds=0.1
    )

    assert target.applied_calls == [("retry-me", OpencodeAction.RETRY)]


async def test_rung3_pauses_intake_on_repeated_recent_failures(db: Database) -> None:
    target = _FakeTarget(outcomes=_FakeOutcomeCounts(failed=2, timeout=1))
    repository = HeartJournalRepository(db)
    handler = _handler(target, repository)

    await handler.handle(
        TickDecision.ACT, _prompt(), tick_number=1, duration_seconds=0.1
    )

    assert target.applied_calls == [(None, OpencodeAction.PAUSE_INTAKE)]
    entry = repository.list_recent(limit=1)[0]
    assert entry.opencode_run_id is None
    assert entry.opencode_action is OpencodeAction.PAUSE_INTAKE


async def test_rung4_escalates_when_cooldown_blocks_a_persisting_fault(
    db: Database,
) -> None:
    """Rung 1 would retry `run-1`, but the cooldown engaged by a prior
    intervention (simulated here by pre-seeding the policy's own cooldown state
    via a first call) blocks it — rung 4 is the deliberate fallback.
    """
    tick_counter = _FakeTickCounter(ticks_run=1)
    policy = _policy(tick_counter=tick_counter, cooldown_ticks=5)
    target = _FakeTarget(retryable=[_run(run_id="run-1")])
    repository = HeartJournalRepository(db)
    brain = _FakeBrainProvider([BrainDone()])
    handler = _handler(
        target, repository, policy=policy, brain=brain, escalate_to_brain_enabled=True
    )

    # First tick: rung 1 fires for real, engaging the cooldown.
    await handler.handle(
        TickDecision.ACT, _prompt(), tick_number=1, duration_seconds=0.1
    )
    assert target.applied_calls == [("run-1", OpencodeAction.RETRY)]

    # Second tick, still within cooldown_ticks, same persisting candidate ->
    # rung 1-3 are cooldown-gated, rung 4 (escalate) fires instead.
    tick_counter.ticks_run = 2
    await handler.handle(
        TickDecision.ACT, _prompt(), tick_number=2, duration_seconds=0.1
    )

    assert len(target.applied_calls) == 1  # unchanged — escalate never calls this
    entries = repository.list_recent(limit=10)
    escalate_entries = [
        e for e in entries if e.opencode_action is OpencodeAction.ESCALATE_TO_BRAIN
    ]
    assert len(escalate_entries) == 1
    assert escalate_entries[0].opencode_run_id == "run-1"
    assert len(brain.requests) == 1


# --- escalate_to_brain: both gates ----------------------------------------------------


async def test_escalate_to_brain_declines_when_the_old_flag_is_off(
    db: Database,
) -> None:
    tick_counter = _FakeTickCounter(ticks_run=1)
    policy = _policy(tick_counter=tick_counter, cooldown_ticks=100)
    target = _FakeTarget(retryable=[_run(run_id="run-1")])
    repository = HeartJournalRepository(db)
    brain = _FakeBrainProvider([BrainDone()])
    handler = _handler(
        target, repository, policy=policy, brain=brain, escalate_to_brain_enabled=False
    )
    await handler.handle(
        TickDecision.ACT, _prompt(), tick_number=1, duration_seconds=0.1
    )
    tick_counter.ticks_run = 2

    await handler.handle(
        TickDecision.ACT, _prompt(), tick_number=2, duration_seconds=0.1
    )

    entries = repository.list_recent(limit=10)
    escalate_entries = [
        e for e in entries if e.opencode_action is OpencodeAction.ESCALATE_TO_BRAIN
    ]
    assert len(escalate_entries) == 1
    assert escalate_entries[0].opencode_outcome is OpencodeInterventionStatus.DECLINED
    assert "escalate_to_brain is false" in escalate_entries[0].opencode_detail
    assert brain.requests == []


async def test_escalate_to_brain_declines_when_no_brain_is_configured(
    db: Database,
) -> None:
    tick_counter = _FakeTickCounter(ticks_run=1)
    policy = _policy(tick_counter=tick_counter, cooldown_ticks=100)
    target = _FakeTarget(retryable=[_run(run_id="run-1")])
    repository = HeartJournalRepository(db)
    handler = _handler(
        target, repository, policy=policy, brain=None, escalate_to_brain_enabled=True
    )
    await handler.handle(
        TickDecision.ACT, _prompt(), tick_number=1, duration_seconds=0.1
    )
    tick_counter.ticks_run = 2

    await handler.handle(
        TickDecision.ACT, _prompt(), tick_number=2, duration_seconds=0.1
    )

    entries = repository.list_recent(limit=10)
    escalate_entries = [
        e for e in entries if e.opencode_action is OpencodeAction.ESCALATE_TO_BRAIN
    ]
    assert len(escalate_entries) == 1
    assert escalate_entries[0].opencode_outcome is OpencodeInterventionStatus.DECLINED
    assert "not enabled" in escalate_entries[0].opencode_detail
    assert escalate_entries[0].brain_status is not None


async def test_escalate_to_brain_journals_both_opencode_and_brain_columns(
    db: Database,
) -> None:
    tick_counter = _FakeTickCounter(ticks_run=1)
    policy = _policy(tick_counter=tick_counter, cooldown_ticks=100)
    target = _FakeTarget(retryable=[_run(run_id="run-1")])
    repository = HeartJournalRepository(db)
    usage = BrainUsage(
        prompt_tokens=10, completion_tokens=5, total_tokens=15, authoritative=True
    )
    brain = _FakeBrainProvider([BrainDone(usage=usage)])
    handler = _handler(
        target, repository, policy=policy, brain=brain, escalate_to_brain_enabled=True
    )
    await handler.handle(
        TickDecision.ACT, _prompt(), tick_number=1, duration_seconds=0.1
    )
    tick_counter.ticks_run = 2

    await handler.handle(
        TickDecision.ACT, _prompt(), tick_number=2, duration_seconds=0.1
    )

    entries = repository.list_recent(limit=10)
    escalate_entries = [
        e for e in entries if e.opencode_action is OpencodeAction.ESCALATE_TO_BRAIN
    ]
    assert len(escalate_entries) == 1
    entry = escalate_entries[0]
    assert entry.opencode_outcome is OpencodeInterventionStatus.APPLIED
    assert entry.brain_prompt_tokens == 10
    assert entry.brain_completion_tokens == 5
    assert target.telemetry_calls[-1][1] is OpencodeAction.ESCALATE_TO_BRAIN


async def test_escalate_to_brain_journals_error_on_a_terminal_brain_error(
    db: Database,
) -> None:
    tick_counter = _FakeTickCounter(ticks_run=1)
    policy = _policy(tick_counter=tick_counter, cooldown_ticks=100)
    target = _FakeTarget(retryable=[_run(run_id="run-1")])
    repository = HeartJournalRepository(db)
    usage = BrainUsage(
        prompt_tokens=4, completion_tokens=0, total_tokens=4, authoritative=False
    )
    brain = _FakeBrainProvider(
        [
            BrainErrorEvent(
                error_class=BrainErrorClass.PROVIDER_SERVER,
                message="upstream 503",
                retryable=True,
                usage=usage,
            )
        ]
    )
    handler = _handler(
        target, repository, policy=policy, brain=brain, escalate_to_brain_enabled=True
    )
    await handler.handle(
        TickDecision.ACT, _prompt(), tick_number=1, duration_seconds=0.1
    )
    tick_counter.ticks_run = 2

    await handler.handle(
        TickDecision.ACT, _prompt(), tick_number=2, duration_seconds=0.1
    )

    entries = repository.list_recent(limit=10)
    escalate_entries = [
        e for e in entries if e.opencode_action is OpencodeAction.ESCALATE_TO_BRAIN
    ]
    assert escalate_entries[0].opencode_outcome is OpencodeInterventionStatus.ERROR
    assert escalate_entries[0].brain_detail == "upstream 503"
    assert escalate_entries[0].brain_prompt_tokens == 4
    assert escalate_entries[0].brain_completion_tokens == 0


async def test_escalate_to_brain_cancellation_propagates_with_no_journal_row(
    db: Database,
) -> None:
    started = asyncio.Event()
    tick_counter = _FakeTickCounter(ticks_run=1)
    policy = _policy(tick_counter=tick_counter, cooldown_ticks=100)
    target = _FakeTarget(retryable=[_run(run_id="run-1")])
    repository = HeartJournalRepository(db)

    class _HangingBrainProvider:
        def stream(self, request: BrainRequest) -> AsyncGenerator[object]:
            async def _gen() -> AsyncGenerator[object]:
                started.set()
                await asyncio.Event().wait()
                yield BrainDone()  # pragma: no cover - unreachable

            return _gen()

        async def aclose(self) -> None:
            pass

    handler = _handler(
        target,
        repository,
        policy=policy,
        brain=_HangingBrainProvider(),  # type: ignore[arg-type]
        escalate_to_brain_enabled=True,
    )
    await handler.handle(
        TickDecision.ACT, _prompt(), tick_number=1, duration_seconds=0.1
    )
    tick_counter.ticks_run = 2

    task = asyncio.create_task(
        handler.handle(TickDecision.ACT, _prompt(), tick_number=2, duration_seconds=0.1)
    )
    await started.wait()
    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("expected CancelledError to propagate out of handle()")

    entries = repository.list_recent(limit=10)
    assert all(
        e.opencode_action is not OpencodeAction.ESCALATE_TO_BRAIN for e in entries
    )
