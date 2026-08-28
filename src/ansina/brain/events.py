"""The `BrainProvider.stream()` data model. See issue #12.

Deliberately a subset of OpenClaw's `start | text_delta | thinking_delta |
toolcall_delta | done | error` union (`docs/architecture/blueprint.md` §1): Ansina has
no tool registry and nothing to think out loud to, so only the events an actual caller
can act on exist — the same "named seam, not speculative surface" posture the
blueprint §2 applies to `ChannelId`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class BrainErrorClass(StrEnum):
    """A stable, provider-agnostic classification for a terminal `BrainErrorEvent`.

    Callers branch on this, never on provider-specific exception types or message text
    — the same reason `TickDecision` exists as a type instead of leaving callers to
    parse the Heart's raw reply (`heart.tick.decision`).
    """

    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    TRANSPORT = "transport"
    PROVIDER_CLIENT = "provider_client"
    PROVIDER_SERVER = "provider_server"
    INTERNAL = "internal"


# Whether a failure of this class is worth retrying at all. A 4xx (bad request, auth,
# not found) will fail identically on retry; an unclassified internal failure is treated
# the same way out of caution — retrying an error this module doesn't understand risks
# repeating whatever caused it.
RETRYABLE_ERROR_CLASSES: frozenset[BrainErrorClass] = frozenset(
    {
        BrainErrorClass.TIMEOUT,
        BrainErrorClass.RATE_LIMIT,
        BrainErrorClass.TRANSPORT,
        BrainErrorClass.PROVIDER_SERVER,
    }
)


@dataclass(frozen=True, slots=True)
class BrainUsage:
    """Token/cost accounting for one `stream()` call, recorded whether or not a
    billing system exists to consume it yet (issue #12's acceptance criteria).

    `authoritative` is `False` when the numbers are estimated because the stream ended
    (successfully or not) before the provider sent its own usage totals — the issue's
    review comment asks this to be recorded explicitly rather than left ambiguous.
    `cost_usd` stays `None` unless `BrainSettings` has pricing configured; no cost
    figure is fabricated from unconfigured assumptions.
    """

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    authoritative: bool
    cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class BrainTextDelta:
    """One chunk of assistant text, in generation order."""

    text: str


@dataclass(frozen=True, slots=True)
class BrainDone:
    """The stream finished successfully. Terminal — no further events follow."""

    usage: BrainUsage | None = None


@dataclass(frozen=True, slots=True)
class BrainErrorEvent:
    """The stream failed. Terminal — no further events follow.

    This is what "never throw after invocation" (blueprint §1) actually looks like in
    Python: a value yielded from the stream, not an exception raised into the caller's
    `async for`. `usage` carries whatever partial accounting exists at the point of
    failure (see `BrainUsage.authoritative`).
    """

    error_class: BrainErrorClass
    message: str
    retryable: bool
    usage: BrainUsage | None = None


BrainEvent = BrainTextDelta | BrainDone | BrainErrorEvent
