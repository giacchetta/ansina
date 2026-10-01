"""`JournalDecisionHandler` — the default `DecisionHandler`. See issue #55.

Writes exactly one `heart_journal` row per completed tick via
`heart.journal.HeartJournalRepository`, offloaded through `anyio.to_thread.run_sync`
the same way every other blocking sqlite call in this codebase is
(`api/authorization.py`'s `require()` is the pattern this follows). Replaces
`LoggingDecisionHandler` as `build_tick_loop`'s default — that handler is kept, not
deleted, as an available alternative (e.g. for a deployment that doesn't want a growing
journal table at all).

The persisted `note` is **code-generated from the triggering snapshot items**, never
the model's own raw reply — see this issue's own scope line: making the note
model-authored is exactly what parked Backlog #15 would need to justify.
"""

from __future__ import annotations

import functools

import anyio.to_thread

from ansina.auth.clock import Clock, iso, utc_now
from ansina.heart.journal import HeartJournalRepository
from ansina.heart.tick.decision import TickDecision
from ansina.heart.tick.snapshot import TickPrompt

# A verbose fault report (many high-priority items, each already bounded by
# `heart.tick.sources.daemon_state`'s own wording) must still never grow a single
# journal row without limit — this is the backstop, not the primary control.
_MAX_NOTE_LENGTH = 500


def _build_note(prompt: TickPrompt) -> str:
    """One line per item in `prompt.items`'s highest-priority *band* — every item
    sharing the maximum priority value actually included, which is also always the
    front of the list (`heart.tick.snapshot.collect_items` sorts highest-priority-
    first, and `build_prompt` never reorders it) — rendered `"<source> reported:
    <text>"`, space-joined, and capped at `_MAX_NOTE_LENGTH`.

    Empty for an empty snapshot (nothing to report — defensive, not expected to be
    reachable on `act`/`escalate` in practice).
    """
    if not prompt.items:
        return ""
    top_priority = max(item.priority for item in prompt.items)
    lines = [
        f"{item.source} reported: {item.text}"
        for item in prompt.items
        if item.priority == top_priority
    ]
    return " ".join(lines)[:_MAX_NOTE_LENGTH]


class JournalDecisionHandler:
    """`handle()` persists one row; `idle` persists an empty note (nothing was
    noticed — the decision itself is the whole record).

    `max_entries`/`retention_days` are resolved once by the caller (`build_tick_loop`,
    from `HeartSettings.journal`) and passed through to every `append()` call — the
    repository itself stays policy-free, mirroring `CredentialRepository.set_password`.
    """

    def __init__(
        self,
        repository: HeartJournalRepository,
        *,
        max_entries: int,
        retention_days: int,
        clock: Clock = utc_now,
    ) -> None:
        self._repository = repository
        self._max_entries = max_entries
        self._retention_days = retention_days
        self._clock = clock

    async def handle(
        self,
        decision: TickDecision,
        prompt: TickPrompt,
        *,
        tick_number: int,
        duration_seconds: float,
    ) -> None:
        note = "" if decision is TickDecision.IDLE else _build_note(prompt)
        await anyio.to_thread.run_sync(
            functools.partial(
                self._repository.append,
                created_at=iso(self._clock()),
                tick_number=tick_number,
                decision=decision,
                note=note,
                prompt_tokens=prompt.tokens,
                duration_seconds=duration_seconds,
                max_entries=self._max_entries,
                retention_days=self._retention_days,
            )
        )
