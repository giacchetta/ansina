"""Real `StateSnapshotSource` implementations for the tick loop. See issue #54.

`heart.tick.snapshot`'s own module docstring says a later issue registers its own
source "the same way milestones register a `Readiness` check" rather than editing the
loop or the snapshot module directly — `daemon_state.DaemonStateSource` is that first
source.
"""

from ansina.heart.tick.sources.daemon_state import (
    DaemonStateSource,
    HealthProbe,
    ReadinessProbe,
    TickStats,
)

__all__ = [
    "DaemonStateSource",
    "HealthProbe",
    "ReadinessProbe",
    "TickStats",
]
