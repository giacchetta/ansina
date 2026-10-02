"""`RecentJournalSource` — feeds the last few `heart_journal` rows back into the next
tick's snapshot. See issue #55.

The Heart's only durable memory: without this, every tick decides from a completely
fresh slate, even though the daemon itself already remembers every past decision in
`heart_journal`. Bounded on every axis that could blow the token budget — row count
(`recent_entries`, `HeartSettings.journal`) and per-row note preview length — folded
into a *single* `SnapshotItem`, so `build_prompt`'s trimming only ever makes one
keep/drop decision about this source, never a partial one that would read as a
truncated, confusing fragment.

`priority` sits below `sources.daemon_state.DaemonStateSource`'s own lowest band
(`_PRIORITY_BACKGROUND = 0`) — recent history is the first thing dropped under budget
pressure, well before anything naming the daemon's *current* state.
"""

from __future__ import annotations

from ansina.heart.journal import HeartJournalRepository, JournalEntry
from ansina.heart.tick.snapshot import SnapshotItem

# Below every priority band `sources.daemon_state` uses (its own floor is
# `_PRIORITY_BACKGROUND = 0`) — the first thing trimmed under budget pressure.
_PRIORITY = -10

# Per-row note preview length inside the rendered line — independent of
# `journal_handler._MAX_NOTE_LENGTH` (that caps what's *stored*; this caps what's
# *replayed*), so a budget change on one side never silently changes the other.
_MAX_NOTE_PREVIEW = 80


def _format_entry(entry: JournalEntry) -> str:
    if entry.note:
        preview = entry.note[:_MAX_NOTE_PREVIEW]
        return f"tick {entry.tick_number}: {entry.decision.value} ({preview})"
    return f"tick {entry.tick_number}: {entry.decision.value}"


class RecentJournalSource:
    """A `StateSnapshotSource` over the last `recent_entries` `heart_journal` rows.

    `recent_entries = 0` (a valid `HeartSettings.journal` value) makes this source
    permanently silent rather than a special case some other module has to branch on.
    """

    name = "recent_journal"

    def __init__(
        self, repository: HeartJournalRepository, *, recent_entries: int
    ) -> None:
        self._repository = repository
        self._recent_entries = recent_entries

    def collect(self) -> tuple[SnapshotItem, ...]:
        if self._recent_entries <= 0:
            return ()
        entries = self._repository.list_recent(limit=self._recent_entries)
        if not entries:
            return ()
        # `list_recent` returns newest-first; replay oldest-first, the order a reader
        # (human or Heart) expects a history line to read in.
        lines = [_format_entry(entry) for entry in reversed(entries)]
        text = "Recent tick history (oldest first): " + " | ".join(lines)
        return (SnapshotItem(source=self.name, text=text, priority=_PRIORITY),)
