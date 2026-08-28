"""Bounded exponential backoff for `BaseBrainProvider`'s retry loop. See issue #12.

Pure function, no I/O — the same posture `heart.tick.loop._next_tick_number` takes, so
the "bounded, never unbounded" property is unit-testable without any real waiting.
"""

from __future__ import annotations


def backoff_seconds(
    attempt: int, *, initial: float, maximum: float, multiplier: float = 2.0
) -> float:
    """The delay before retry attempt `attempt` (1-indexed: the delay before the
    *first* retry, after the initial attempt failed, is `backoff_seconds(1, ...)`).

    Grows geometrically from `initial`, capped at `maximum` — a bounded ceiling, not a
    target, the same shape `HeartSettings.context_tokens` gives the Heart's prompt
    budget.
    """
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    return min(maximum, initial * (multiplier ** (attempt - 1)))
