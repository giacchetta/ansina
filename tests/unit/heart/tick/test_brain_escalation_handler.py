from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence

from ansina.brain.events import BrainDone, BrainErrorClass, BrainErrorEvent, BrainUsage
from ansina.brain.provider import BrainRequest
from ansina.heart.journal import BrainEscalationStatus, HeartJournalRepository
from ansina.heart.tick.brain_escalation_handler import BrainEscalationHandler
from ansina.heart.tick.decision import TickDecision
from ansina.heart.tick.snapshot import SnapshotItem, TickPrompt
from ansina.storage.database import Database


def _prompt(text: str = "snapshot text") -> TickPrompt:
    return TickPrompt(
        text=text,
        tokens=len(text),
        items_included=1,
        items_dropped=0,
        items=(SnapshotItem(source="daemon_state", text=text, priority=100),),
    )


class _FakeBrainProvider:
    """A minimal `BrainProvider`-shaped fake: `stream()` yields whatever `events`
    holds (or raises mid-generator if an item is a `BaseException`). `aclose()` is
    the *provider's* own cleanup (closed by `create_app`'s lifespan, not by this
    handler — `BrainEscalationHandler` only ever closes the per-call *generator*
    `stream()` returns, never the provider itself), so it's a no-op here.
    """

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
    brain: _FakeBrainProvider | None,
    repository: HeartJournalRepository,
    *,
    max_entries: int = 1000,
    retention_days: int = 365,
) -> BrainEscalationHandler:
    return BrainEscalationHandler(
        brain,  # type: ignore[arg-type]
        repository,
        model="test-model",
        max_output_tokens=64,
        max_entries=max_entries,
        retention_days=retention_days,
    )


# --- idle/act: no-op, regardless of whether a brain is configured -------------------


async def test_idle_is_a_no_op(db: Database) -> None:
    brain = _FakeBrainProvider([BrainDone()])
    repository = HeartJournalRepository(db)
    handler = _handler(brain, repository)

    await handler.handle(
        TickDecision.IDLE, _prompt(), tick_number=1, duration_seconds=0.1
    )

    assert brain.requests == []
    assert repository.list_recent(limit=10) == []


async def test_act_is_a_no_op(db: Database) -> None:
    brain = _FakeBrainProvider([BrainDone()])
    repository = HeartJournalRepository(db)
    handler = _handler(brain, repository)

    await handler.handle(
        TickDecision.ACT, _prompt(), tick_number=1, duration_seconds=0.1
    )

    assert brain.requests == []
    assert repository.list_recent(limit=10) == []


# --- escalate, no BrainProvider configured: declined, never touches it -------------


async def test_escalate_with_no_brain_configured_journals_declined(
    db: Database,
) -> None:
    repository = HeartJournalRepository(db)
    handler = _handler(None, repository)

    await handler.handle(
        TickDecision.ESCALATE, _prompt(), tick_number=3, duration_seconds=0.2
    )

    entries = repository.list_recent(limit=10)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.tick_number == 3
    assert entry.decision is TickDecision.ESCALATE
    assert entry.brain_status is BrainEscalationStatus.DECLINED
    assert "not enabled" in entry.brain_detail
    assert entry.brain_prompt_tokens is None
    assert entry.brain_completion_tokens is None


# --- escalate, a successful stream ---------------------------------------------------


async def test_escalate_successful_stream_journals_called_with_usage(
    db: Database,
) -> None:
    usage = BrainUsage(
        prompt_tokens=10, completion_tokens=5, total_tokens=15, authoritative=True
    )
    brain = _FakeBrainProvider([BrainDone(usage=usage)])
    repository = HeartJournalRepository(db)
    handler = _handler(brain, repository)
    prompt = _prompt("the triggering prompt")

    await handler.handle(
        TickDecision.ESCALATE, prompt, tick_number=7, duration_seconds=1.0
    )

    assert len(brain.requests) == 1
    assert brain.requests[0].messages[0].content == prompt.text

    entries = repository.list_recent(limit=10)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.tick_number == 7
    assert entry.decision is TickDecision.ESCALATE
    assert entry.brain_status is BrainEscalationStatus.CALLED
    assert entry.brain_detail == ""
    assert entry.brain_prompt_tokens == 10
    assert entry.brain_completion_tokens == 5
    # The Heart's own prompt token count, reused on this second row — distinct from
    # the Brain's own usage above.
    assert entry.prompt_tokens == prompt.tokens


async def test_escalate_successful_stream_with_no_usage_journals_null_tokens(
    db: Database,
) -> None:
    brain = _FakeBrainProvider([BrainDone(usage=None)])
    repository = HeartJournalRepository(db)
    handler = _handler(brain, repository)

    await handler.handle(
        TickDecision.ESCALATE, _prompt(), tick_number=1, duration_seconds=0.1
    )

    entry = repository.list_recent(limit=1)[0]
    assert entry.brain_status is BrainEscalationStatus.CALLED
    assert entry.brain_prompt_tokens is None
    assert entry.brain_completion_tokens is None


# --- escalate, a terminal BrainErrorEvent -------------------------------------------


async def test_escalate_terminal_error_journals_error_with_detail(db: Database) -> None:
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
    repository = HeartJournalRepository(db)
    handler = _handler(brain, repository)

    await handler.handle(
        TickDecision.ESCALATE, _prompt(), tick_number=2, duration_seconds=0.3
    )

    entry = repository.list_recent(limit=1)[0]
    assert entry.brain_status is BrainEscalationStatus.ERROR
    assert entry.brain_detail == "upstream 503"
    assert entry.brain_prompt_tokens == 4
    assert entry.brain_completion_tokens == 0


async def test_escalate_error_detail_is_truncated(db: Database) -> None:
    brain = _FakeBrainProvider(
        [
            BrainErrorEvent(
                error_class=BrainErrorClass.INTERNAL,
                message="x" * 1000,
                retryable=False,
            )
        ]
    )
    repository = HeartJournalRepository(db)
    handler = _handler(brain, repository)

    await handler.handle(
        TickDecision.ESCALATE, _prompt(), tick_number=1, duration_seconds=0.1
    )

    entry = repository.list_recent(limit=1)[0]
    assert len(entry.brain_detail) == 500


# --- escalate: never raises a Brain-side failure into the caller -------------------


async def test_escalate_never_raises_on_a_brain_error_event(db: Database) -> None:
    """The handler's own contract (mirroring issue #12's own invariant on the
    provider side): a terminal `BrainErrorEvent` is journaled, never re-raised.
    """
    brain = _FakeBrainProvider(
        [
            BrainErrorEvent(
                error_class=BrainErrorClass.TIMEOUT, message="timed out", retryable=True
            )
        ]
    )
    repository = HeartJournalRepository(db)
    handler = _handler(brain, repository)

    # Must not raise.
    await handler.handle(
        TickDecision.ESCALATE, _prompt(), tick_number=1, duration_seconds=0.1
    )


# --- escalate: cancellation mid-stream propagates, no row is written ---------------


async def test_cancellation_mid_stream_propagates_and_writes_no_row(
    db: Database,
) -> None:
    started = asyncio.Event()

    class _HangingBrainProvider:
        def stream(self, request: BrainRequest) -> AsyncGenerator[object]:
            async def _gen() -> AsyncGenerator[object]:
                started.set()
                await asyncio.Event().wait()  # never completes on its own
                yield BrainDone()  # pragma: no cover - unreachable

            return _gen()

        async def aclose(self) -> None:
            pass

    brain = _HangingBrainProvider()
    repository = HeartJournalRepository(db)
    handler = _handler(brain, repository)  # type: ignore[arg-type]

    task = asyncio.create_task(
        handler.handle(
            TickDecision.ESCALATE, _prompt(), tick_number=1, duration_seconds=0.1
        )
    )
    await started.wait()
    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("expected CancelledError to propagate out of handle()")

    assert repository.list_recent(limit=10) == []
