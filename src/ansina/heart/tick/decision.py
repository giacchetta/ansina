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


def parse_decision(raw: str) -> TickDecision:
    """The first recognizable decision word in `raw`, defaulting to `IDLE`.

    An autonomous loop must never guess toward `ACT` or `ESCALATE` — an unparseable or
    ambiguous reply (empty output, a hedge, a refusal) resolves to the inert choice,
    with a warning so the prompt or the model can be fixed rather than silently
    misread.
    """
    normalized = raw.strip().lower()
    first_word = normalized.split(maxsplit=1)[0] if normalized else ""
    first_word = first_word.strip(".,:;!?\"'")
    try:
        return TickDecision(first_word)
    except ValueError:
        logger.warning(
            "heart tick: unparseable decision, defaulting to idle",
            extra={"raw": raw},
        )
        return TickDecision.IDLE
