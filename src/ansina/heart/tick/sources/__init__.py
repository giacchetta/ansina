"""Real `StateSnapshotSource` implementations for the tick loop. See issues
#54/#55/#78.

`heart.tick.snapshot`'s own module docstring says a later issue registers its own
source "the same way milestones register a `Readiness` check" rather than editing the
loop or the snapshot module directly — `daemon_state.DaemonStateSource` was that first
source; `recent_journal.RecentJournalSource` (issue #55) is the second;
`opencode_state.OpencodeStateSource` (issue #78) is the third.
"""

from ansina.heart.tick.sources.daemon_state import (
    DaemonStateSource,
    HealthProbe,
    ReadinessProbe,
    TickStats,
)
from ansina.heart.tick.sources.opencode_state import (
    OpencodeStateProbe,
    OpencodeStateSource,
    OutcomeCounts,
    RunningRunState,
)
from ansina.heart.tick.sources.recent_journal import RecentJournalSource

__all__ = [
    "DaemonStateSource",
    "HealthProbe",
    "OpencodeStateProbe",
    "OpencodeStateSource",
    "OutcomeCounts",
    "ReadinessProbe",
    "RecentJournalSource",
    "RunningRunState",
    "TickStats",
]
