"""Remote Brain provider port: the 35B+ reasoning model reached over the network. See
issue #12.

`BrainProvider` is the port; `build_brain_provider()` builds the one configured adapter
(or raises `BrainUnavailableError` loudly). Nothing in this milestone calls `stream()`
yet — the tick loop's `escalate` branch (`heart.tick.loop.LoggingDecisionHandler`) stays
log-only until a follow-up issue wires it to this port, the same way `heart.tick`
followed `heart.runtime` in its own milestone. `create_app()` (`ansina.api.app`) owns
construction and `aclose()`, the same way it owns `HeartRuntime` and `Database`.
"""

from ansina.brain.events import (
    RETRYABLE_ERROR_CLASSES,
    BrainDone,
    BrainErrorClass,
    BrainErrorEvent,
    BrainEvent,
    BrainTextDelta,
    BrainUsage,
)
from ansina.brain.provider import (
    BaseBrainProvider,
    BrainMessage,
    BrainProvider,
    BrainRequest,
    BrainRole,
)
from ansina.brain.selection import BrainUnavailableError, build_brain_provider

__all__ = [
    "RETRYABLE_ERROR_CLASSES",
    "BaseBrainProvider",
    "BrainDone",
    "BrainErrorClass",
    "BrainErrorEvent",
    "BrainEvent",
    "BrainMessage",
    "BrainProvider",
    "BrainRequest",
    "BrainRole",
    "BrainTextDelta",
    "BrainUnavailableError",
    "BrainUsage",
    "build_brain_provider",
]
