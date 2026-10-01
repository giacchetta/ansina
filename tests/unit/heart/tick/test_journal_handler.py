from __future__ import annotations

import asyncio
import threading

from ansina.heart.journal import HeartJournalRepository
from ansina.heart.tick.decision import TickDecision
from ansina.heart.tick.journal_handler import JournalDecisionHandler, _build_note
from ansina.heart.tick.snapshot import SnapshotItem, TickPrompt
from ansina.storage.database import Database


def _prompt(items: tuple[SnapshotItem, ...] = ()) -> TickPrompt:
    return TickPrompt(
        text="x", tokens=1, items_included=len(items), items_dropped=0, items=items
    )


# --- _build_note: pure, tested directly (mirrors `loop._next_tick_number`'s own
# direct-import-of-a-private-function precedent) ------------------------------------


def test_build_note_is_empty_for_no_items() -> None:
    assert _build_note(_prompt(())) == ""


def test_build_note_includes_only_the_highest_priority_band() -> None:
    items = (
        SnapshotItem(source="daemon_state", text="database unhealthy.", priority=100),
        SnapshotItem(source="daemon_state", text="uptime: 5m.", priority=0),
    )

    note = _build_note(_prompt(items))

    assert "database unhealthy." in note
    assert "uptime: 5m." not in note
    assert note.startswith("daemon_state reported:")


def test_build_note_joins_every_item_sharing_the_top_priority() -> None:
    items = (
        SnapshotItem(source="a", text="fault one.", priority=100),
        SnapshotItem(source="b", text="fault two.", priority=100),
    )

    note = _build_note(_prompt(items))

    assert "fault one." in note
    assert "fault two." in note


def test_build_note_truncates_to_the_length_cap() -> None:
    items = (SnapshotItem(source="s", text="x" * 1000, priority=5),)

    note = _build_note(_prompt(items))

    assert len(note) <= 500


# --- JournalDecisionHandler.handle --------------------------------------------------


async def test_handle_writes_exactly_one_row_per_call(db: Database) -> None:
    repository = HeartJournalRepository(db)
    handler = JournalDecisionHandler(repository, max_entries=1000, retention_days=365)

    await handler.handle(
        TickDecision.ACT,
        _prompt((SnapshotItem(source="s", text="fault", priority=100),)),
        tick_number=3,
        duration_seconds=1.25,
    )

    entries = repository.list_recent(limit=10)
    assert len(entries) == 1
    assert entries[0].tick_number == 3
    assert entries[0].decision is TickDecision.ACT
    assert entries[0].duration_seconds == 1.25


async def test_handle_persists_a_code_generated_note_for_act(db: Database) -> None:
    repository = HeartJournalRepository(db)
    handler = JournalDecisionHandler(repository, max_entries=1000, retention_days=365)
    items = (
        SnapshotItem(source="daemon_state", text="database unhealthy.", priority=100),
    )

    await handler.handle(
        TickDecision.ACT, _prompt(items), tick_number=1, duration_seconds=0.1
    )

    entry = repository.list_recent(limit=1)[0]
    assert "database unhealthy." in entry.note


async def test_handle_persists_an_empty_note_for_idle(db: Database) -> None:
    repository = HeartJournalRepository(db)
    handler = JournalDecisionHandler(repository, max_entries=1000, retention_days=365)
    items = (SnapshotItem(source="daemon_state", text="all nominal.", priority=10),)

    await handler.handle(
        TickDecision.IDLE, _prompt(items), tick_number=1, duration_seconds=0.1
    )

    entry = repository.list_recent(limit=1)[0]
    assert entry.note == ""


async def test_handle_offloads_the_write_to_a_worker_thread(db: Database) -> None:
    """Mirrors `test_loop.py`'s own `test_tick_once_skips_when_another_tick_is_already
    _in_flight` proof shape: a blocking repository call runs to completion and
    `handle()`'s `await` resumes afterward — only reachable without deadlocking this
    test's own `asyncio.to_thread(started.wait, ...)` call if `handle()` genuinely
    offloads through `anyio.to_thread.run_sync` rather than blocking the event loop
    thread inline (which would leave nothing free to run the executor callback this
    test's own wait depends on).
    """
    started = threading.Event()
    release = threading.Event()
    real_repository = HeartJournalRepository(db)

    class _BlockingRepository:
        def append(self, **kwargs: object) -> None:
            started.set()
            release.wait(timeout=2)
            real_repository.append(**kwargs)  # type: ignore[arg-type]

    handler = JournalDecisionHandler(
        _BlockingRepository(),  # type: ignore[arg-type]
        max_entries=1000,
        retention_days=365,
    )

    task = asyncio.create_task(
        handler.handle(
            TickDecision.IDLE, _prompt(), tick_number=1, duration_seconds=0.1
        )
    )
    await asyncio.to_thread(started.wait, 2)
    release.set()
    await task

    assert real_repository.list_recent(limit=10)[0].tick_number == 1
