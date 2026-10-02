"""Bounded state snapshot for the tick prompt. See issue #11.

The Heart's 8k context is a hard ceiling (blueprint §3), not a target — this module is
what makes that a proven property of the tick loop rather than an assumption. Every
prompt `build_prompt` returns is measured with the *runtime's own* tokenizer
(`HeartRuntime.token_count`) and trimmed to fit `budget_tokens` before it is ever handed
to `generate()`; `BaseHeartRuntime`'s own `HeartContextOverflowError`
(`heart/runtime.py`) stays as a backstop, not the mechanism.

No `StateSnapshotSource` ships registered here — there is no pending-work, schedule, or
event domain yet (`storage/` has only `schema_version`). A later issue registers its own
source the same way milestones register a `Readiness` check (`api/readiness.py`) instead
of editing this module or the loop.

The prompt template itself lives in `heart.tick.prompts` (issue #53) — extracted so a
prompt A/B/C bench (`heart.eval`) is a parameter, not an edit here.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from ansina.heart.tick.prompts import DEFAULT_TEMPLATE, NO_PENDING_STATE
from ansina.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SnapshotItem:
    """One unit of state a `StateSnapshotSource` contributes to a tick.

    `priority` is ascending importance — items are kept highest-`priority`-first when
    the snapshot must be trimmed to fit the token budget.
    """

    source: str
    text: str
    priority: int = 0


class StateSnapshotSource(Protocol):
    """A pluggable contributor to the tick snapshot — structural, like `HeartRuntime`.

    Nothing implements this yet; it exists so a later issue (pending work, schedules,
    recent events) has a seam to register into rather than one to invent.
    """

    name: str

    def collect(self) -> Sequence[SnapshotItem]:
        """Current items from this source. May be empty. Must not block for long —
        every tick pays for however long every registered source takes to answer.
        """
        ...


def collect_items(sources: Sequence[StateSnapshotSource]) -> list[SnapshotItem]:
    """Every item from every source, ordered highest-priority-first.

    A source that raises is logged and skipped — one broken source must never stop a
    tick, the same fault-isolation posture `TickLoop` gives to the tick itself.
    """
    items: list[SnapshotItem] = []
    for source in sources:
        try:
            items.extend(source.collect())
        except Exception:
            logger.exception(
                "heart tick: snapshot source failed, skipping",
                extra={"source": source.name},
            )
    items.sort(key=lambda item: item.priority, reverse=True)
    return items


@dataclass(frozen=True, slots=True)
class TickPrompt:
    """The rendered prompt handed to `generate()`, plus what it cost to build.

    `items` (issue #55) is the `SnapshotItem`s that actually survived trimming, in the
    same highest-priority-first order they were rendered in — `JournalDecisionHandler`
    derives its code-generated `act`/`escalate` note from this rather than from the
    model's own raw reply. Defaulted to `()` so every pre-#55 `TickPrompt(...)`
    construction (e.g. in tests) stays valid.
    """

    text: str
    tokens: int
    items_included: int
    items_dropped: int
    items: tuple[SnapshotItem, ...] = ()


def build_prompt(
    items: Sequence[SnapshotItem],
    *,
    budget_tokens: int,
    token_count: Callable[[str], int],
    template: str = DEFAULT_TEMPLATE,
) -> TickPrompt:
    """Render `items` into the tick prompt, trimming lowest-priority-first to fit
    `budget_tokens`.

    `budget_tokens` is `HeartSettings.context_tokens - HeartSettings.max_output_tokens`
    (derived by the caller) — the same headroom `BaseHeartRuntime.generate` reserves for
    its own refusal, so a prompt built here should never trip
    `HeartContextOverflowError` in practice.

    `template` defaults to `heart.tick.prompts.DEFAULT_TEMPLATE` (the shipped, unchanged
    framing) — every caller before issue #53 passed nothing, so the default keeps every
    existing call site and test byte-for-byte unaffected. `heart.eval`'s bench is the
    first caller to pass a different variant.
    """
    template_tokens = token_count(template.format(state=NO_PENDING_STATE))
    available = max(0, budget_tokens - template_tokens)

    included: list[SnapshotItem] = []
    used = 0
    dropped = 0
    for index, item in enumerate(items):
        cost = token_count(item.text)
        if used + cost > available:
            dropped = len(items) - index
            break
        included.append(item)
        used += cost

    if dropped:
        logger.warning(
            "heart tick: snapshot truncated to fit the token budget",
            extra={"items_dropped": dropped, "budget_tokens": budget_tokens},
        )

    state = "\n".join(item.text for item in included) if included else NO_PENDING_STATE
    text = template.format(state=state)
    return TickPrompt(
        text=text,
        tokens=token_count(text),
        items_included=len(included),
        items_dropped=dropped,
        items=tuple(included),
    )
