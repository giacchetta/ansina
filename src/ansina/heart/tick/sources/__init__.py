"""Real `StateSnapshotSource` implementations for the tick loop. See issues #54/#55.

`heart.tick.snapshot`'s own module docstring says a later issue registers its own
source "the same way milestones register a `Readiness` check" rather than editing the
loop or the snapshot module directly — `daemon_state.DaemonStateSource` was that first
source; `recent_journal.RecentJournalSource` (issue #55) is the second.
"""

from ansina.heart.tick.sources.daemon_state import (
    DaemonStateSource,
    HealthProbe,
    ReadinessProbe,
    TickStats,
)
from ansina.heart.tick.sources.recent_journal import RecentJournalSource

__all__ = [
    "DaemonStateSource",
    "HealthProbe",
    "ReadinessProbe",
    "RecentJournalSource",
    "TickStats",
]
