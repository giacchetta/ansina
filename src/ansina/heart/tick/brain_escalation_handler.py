"""`BrainEscalationHandler` — the second `DecisionHandler`, composed alongside
`JournalDecisionHandler` by `build_tick_loop` when `[heart.tick] escalate_to_brain` is
`True`. See issue #64.

Wires the tick loop's own `escalate` decision to `BrainProvider.stream()` (issue #12)
— the hand-off issue #11 always said would eventually come once #12's port existed.
M6's own multi-hour soak (#56, closed out by #60) found zero false `escalate`s over
960 real ticks in a healthy steady state and concluded no dedup/cooldown rung is
needed before wiring this — see `docs/architecture/blueprint.md` §3 and `AGENTS.md`'s
M7 #64 entry for that reasoning. No such machinery is built here, deliberately.

Only `TickDecision.ESCALATE` reaches the Brain; `idle`/`act` are no-ops — `act` has
nothing to act on yet either (see `LoggingDecisionHandler`'s own docstring).

`BrainProvider.stream()`'s own invariant (issue #12) — returns synchronously, never
throws after invocation, every failure arrives as a terminal `BrainErrorEvent` — is
what makes this handler **never raise into the tick loop** on a Brain-side failure: a
Brain outage is a different failure domain from a tick fault, and must never trip
issue #54's circuit breaker. `asyncio.CancelledError` (the caller aborting mid-stream,
e.g. `TickLoop.stop()` during shutdown while a call is in flight) is the one exception
this handler lets propagate untouched — `BaseBrainProvider._run`'s own module
docstring documents the identical reasoning: it is the caller's own signal, not a
Brain failure, and swallowing it would break structured concurrency. No journal row is
written for an attempt that was cancelled mid-stream — the triggering tick's own row
(written by `JournalDecisionHandler`, ahead of this handler in
`CompositeDecisionHandler`'s order) already recorded what triggered the escalation.

Every outcome is journaled through the existing `HeartJournalRepository.append()` path
— a second row sharing the triggering tick's own `tick_number` and
`decision=TickDecision.ESCALATE` (`heart_journal.decision`'s own `CHECK` already allows
that value; no schema change needed there), with the new `brain_*` columns (issue #64's
migration) filled in, and every pre-existing column reused for what it already means:
`prompt_tokens` is the same triggering prompt's own token count (the Heart's, not the
Brain's — `brain_prompt_tokens` is that), and `duration_seconds` is this call's own
wall-clock cost, not the tick's. A second row rather than an update to the first:
`HeartJournalRepository` is deliberately append-only (`append`/`list_recent`/`sweep`,
no update method — see that module's own docstring) and this issue's own scope says to
compose a second handler, not rewrite `JournalDecisionHandler`'s own one-row-per-tick
contract.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable

import anyio.to_thread

from ansina.auth.clock import Clock, iso, utc_now
from ansina.brain.events import BrainDone, BrainErrorEvent
from ansina.brain.provider import BrainMessage, BrainProvider, BrainRequest
from ansina.heart.journal import BrainEscalationStatus, HeartJournalRepository
from ansina.heart.tick.decision import TickDecision
from ansina.heart.tick.snapshot import TickPrompt
from ansina.logging import get_logger

logger = get_logger(__name__)

# Mirrors `journal_handler._MAX_NOTE_LENGTH`'s own reasoning: a Brain error message is
# provider-controlled text, not Ansina's own, and must still never grow a journal row
# without limit — this is the backstop, not the primary control.
_MAX_DETAIL_LENGTH = 500

_DECLINED_NO_BRAIN = "declined: [brain] is not enabled or configured"


class BrainEscalationHandler:
    """`handle()` is a no-op for `idle`/`act`; for `escalate`, see the module
    docstring. `brain` may be `None` (the feature flag is on but `[brain]` itself
    isn't configured) — that's `declined`, not an error, and never touches
    `BrainProvider` at all.
    """

    def __init__(
        self,
        brain: BrainProvider | None,
        repository: HeartJournalRepository,
        *,
        model: str,
        max_output_tokens: int,
        max_entries: int,
        retention_days: int,
        clock: Clock = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._brain = brain
        self._repository = repository
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
        if decision is not TickDecision.ESCALATE:
            return

        if self._brain is None:
            logger.warning(
                "heart tick: escalate declined, no BrainProvider configured",
                extra={"tick": tick_number},
            )
            await self._journal(
                tick_number,
                prompt,
                status=BrainEscalationStatus.DECLINED,
                detail=_DECLINED_NO_BRAIN,
                prompt_tokens=None,
                completion_tokens=None,
                call_duration=0.0,
            )
            return

        request = BrainRequest(
            messages=(BrainMessage(role="user", content=prompt.text),),
            model=self._model,
            max_output_tokens=self._max_output_tokens,
        )
        status = BrainEscalationStatus.CALLED
        detail = ""
        brain_prompt_tokens: int | None = None
        brain_completion_tokens: int | None = None
        start = self._monotonic()
        gen = self._brain.stream(request)
        try:
            # `asyncio.CancelledError` is deliberately not caught here — see the
            # module docstring's own cancellation paragraph. It propagates straight
            # out of this `async for`, through `finally: await gen.aclose()` below
            # (safe — cancelling a generator mid-iteration and then closing it is the
            # normal shutdown path), and out of `handle()` with no journal row written.
            async for event in gen:
                if isinstance(event, BrainDone):
                    if event.usage is not None:
                        brain_prompt_tokens = event.usage.prompt_tokens
                        brain_completion_tokens = event.usage.completion_tokens
                elif isinstance(event, BrainErrorEvent):
                    status = BrainEscalationStatus.ERROR
                    detail = event.message[:_MAX_DETAIL_LENGTH]
                    if event.usage is not None:
                        brain_prompt_tokens = event.usage.prompt_tokens
                        brain_completion_tokens = event.usage.completion_tokens
        finally:
            await gen.aclose()
        call_duration = self._monotonic() - start

        logger.info(
            "heart tick: escalate reached the Brain",
            extra={
                "tick": tick_number,
                "status": status.value,
                "duration_seconds": call_duration,
            },
        )
        await self._journal(
            tick_number,
            prompt,
            status=status,
            detail=detail,
            prompt_tokens=brain_prompt_tokens,
            completion_tokens=brain_completion_tokens,
            call_duration=call_duration,
        )

    async def _journal(
        self,
        tick_number: int,
        prompt: TickPrompt,
        *,
        status: BrainEscalationStatus,
        detail: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        call_duration: float,
    ) -> None:
        await anyio.to_thread.run_sync(
            functools.partial(
                self._repository.append,
                created_at=iso(self._clock()),
                tick_number=tick_number,
                decision=TickDecision.ESCALATE,
                note="",
                prompt_tokens=prompt.tokens,
                duration_seconds=call_duration,
                max_entries=self._max_entries,
                retention_days=self._retention_days,
                brain_status=status,
                brain_detail=detail,
                brain_prompt_tokens=prompt_tokens,
                brain_completion_tokens=completion_tokens,
            )
        )
