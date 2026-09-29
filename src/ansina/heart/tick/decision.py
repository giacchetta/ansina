"""The tick loop's only output: idle / act / escalate. See issue #11.

The Heart's raw `generate()` text is free-form model output, not a typed value — this
module is the one seam that turns it into something the loop can safely branch on.
"""

from __future__ import annotations

from enum import StrEnum

from ansina.logging import get_logger

logger = get_logger(__name__)


class TickDecision(StrEnum):
    """The Heart's day-one duty, restated as a type: nothing else is a valid answer."""

    IDLE = "idle"
    ACT = "act"
    ESCALATE = "escalate"


def try_parse_decision(raw: str) -> TickDecision | None:
    """The first recognizable decision word in `raw`, or `None` if unparseable.

    Pure — no logging, no default. `parse_decision` below is the production wrapper
    every tick loop call site uses; this is the seam issue #53's bench needs so it can
    tell a *correct* `idle` apart from a *fallback* `idle` (`parse_decision` collapses
    that distinction by design, which is exactly why the loop should keep using it and
    the bench should not).

    Strips everything up to and including the last `</think>` first (issue #53's
    first chat-templated bench run, measured: a reasoning-tuned model's decision word
    lands *after* its own closing think tag, and the plain first-word rule below would
    otherwise match a word from the reasoning trace itself — "Thinking" is never a
    `TickDecision`, but a wandering CoT can easily contain one of the three real words
    ahead of the model's actual final answer). A reply with no `</think>` at all
    (every non-reasoning model, and the pre-#53 raw-prompt path) is completely
    unaffected — `rsplit` with no match returns the original string unchanged.
    """
    after_thinking = raw.rsplit("</think>", 1)[-1]
    normalized = after_thinking.strip().lower()
    first_word = normalized.split(maxsplit=1)[0] if normalized else ""
    first_word = first_word.strip(".,:;!?\"'")
    try:
        return TickDecision(first_word)
    except ValueError:
        return None


def parse_decision(raw: str) -> TickDecision:
    """The first recognizable decision word in `raw`, defaulting to `IDLE`.

    An autonomous loop must never guess toward `ACT` or `ESCALATE` — an unparseable or
    ambiguous reply (empty output, a hedge, a refusal) resolves to the inert choice,
    with a warning so the prompt or the model can be fixed rather than silently
    misread.
    """
    decision = try_parse_decision(raw)
    if decision is None:
        logger.warning(
            "heart tick: unparseable decision, defaulting to idle",
            extra={"raw": raw},
        )
        return TickDecision.IDLE
    return decision
