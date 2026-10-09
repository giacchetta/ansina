from __future__ import annotations

from datetime import UTC, datetime

from ansina.auth.clock import iso
from ansina.heart.journal import (
    BrainEscalationStatus,
    HeartJournalRepository,
    JournalEntry,
)
from ansina.heart.tick.decision import TickDecision
from ansina.storage.database import Database

_NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _append(
    repository: HeartJournalRepository,
    *,
    created_at: str,
    tick_number: int = 1,
    decision: TickDecision = TickDecision.IDLE,
    note: str = "",
    max_entries: int = 1000,
    retention_days: int = 365,
) -> JournalEntry:
    return repository.append(
        created_at=created_at,
        tick_number=tick_number,
        decision=decision,
        note=note,
        prompt_tokens=10,
        duration_seconds=0.5,
        max_entries=max_entries,
        retention_days=retention_days,
    )


def test_append_returns_a_fully_populated_entry(db: Database) -> None:
    repository = HeartJournalRepository(db)

    entry = _append(
        repository,
        created_at=iso(_NOW),
        tick_number=7,
        decision=TickDecision.ACT,
        note="daemon_state reported: database unhealthy.",
    )

    assert entry.id
    assert entry.created_at == iso(_NOW)
    assert entry.tick_number == 7
    assert entry.decision is TickDecision.ACT
    assert entry.note == "daemon_state reported: database unhealthy."
    assert entry.prompt_tokens == 10
    assert entry.duration_seconds == 0.5
    # No Brain interaction on this row — see the dedicated `brain_*` tests below.
    assert entry.brain_status is None
    assert entry.brain_detail == ""
    assert entry.brain_prompt_tokens is None
    assert entry.brain_completion_tokens is None


def test_append_with_brain_outcome_persists_and_reads_back(db: Database) -> None:
    """Issue #64's four new optional params — every pre-#64 caller (the test above)
    is unaffected by their defaults; this is the one test that exercises them.
    """
    repository = HeartJournalRepository(db)

    entry = repository.append(
        created_at=iso(_NOW),
        tick_number=3,
        decision=TickDecision.ESCALATE,
        note="",
        prompt_tokens=10,
        duration_seconds=1.5,
        max_entries=1000,
        retention_days=365,
        brain_status=BrainEscalationStatus.CALLED,
        brain_detail="",
        brain_prompt_tokens=42,
        brain_completion_tokens=7,
    )

    assert entry.brain_status is BrainEscalationStatus.CALLED
    assert entry.brain_prompt_tokens == 42
    assert entry.brain_completion_tokens == 7

    reloaded = repository.list_recent(limit=1)[0]
    assert reloaded.brain_status is BrainEscalationStatus.CALLED
    assert reloaded.brain_prompt_tokens == 42
    assert reloaded.brain_completion_tokens == 7


def test_append_generates_a_unique_id_per_row(db: Database) -> None:
    repository = HeartJournalRepository(db)

    first = _append(repository, created_at=iso(_NOW))
    second = _append(repository, created_at=iso(_NOW))

    assert first.id != second.id


def test_list_recent_orders_newest_first(db: Database) -> None:
    repository = HeartJournalRepository(db)
    t1 = iso(_NOW)
    t2 = iso(_NOW.replace(minute=1))
    t3 = iso(_NOW.replace(minute=2))

    _append(repository, created_at=t1, tick_number=1)
    _append(repository, created_at=t2, tick_number=2)
    _append(repository, created_at=t3, tick_number=3)

    entries = repository.list_recent(limit=10)

    assert [e.tick_number for e in entries] == [3, 2, 1]


def test_list_recent_respects_limit(db: Database) -> None:
    repository = HeartJournalRepository(db)
    for minute in range(5):
        _append(repository, created_at=iso(_NOW.replace(minute=minute)))

    entries = repository.list_recent(limit=2)

    assert len(entries) == 2


def test_list_recent_filters_by_since(db: Database) -> None:
    repository = HeartJournalRepository(db)
    old = iso(_NOW)
    new = iso(_NOW.replace(minute=5))
    _append(repository, created_at=old, tick_number=1)
    _append(repository, created_at=new, tick_number=2)

    entries = repository.list_recent(limit=10, since=old)

    assert [e.tick_number for e in entries] == [2]


def test_list_recent_on_an_empty_table_returns_an_empty_list(db: Database) -> None:
    assert HeartJournalRepository(db).list_recent(limit=10) == []


def test_sweep_deletes_rows_older_than_retention_days(db: Database) -> None:
    repository = HeartJournalRepository(db)
    old = iso(_NOW)
    new = iso(_NOW.replace(day=10))
    repository.append(
        created_at=old,
        tick_number=1,
        decision=TickDecision.IDLE,
        note="",
        prompt_tokens=1,
        duration_seconds=0.1,
        max_entries=1000,
        retention_days=365,  # no sweep pressure from this call itself
    )
    repository.append(
        created_at=new,
        tick_number=2,
        decision=TickDecision.IDLE,
        note="",
        prompt_tokens=1,
        duration_seconds=0.1,
        max_entries=1000,
        retention_days=365,
    )

    deleted = repository.sweep(_NOW.replace(day=10), max_entries=1000, retention_days=5)

    assert deleted == 1
    remaining = repository.list_recent(limit=10)
    assert [e.tick_number for e in remaining] == [2]


def test_sweep_keeps_only_the_newest_max_entries(db: Database) -> None:
    repository = HeartJournalRepository(db)
    for minute in range(5):
        repository.append(
            created_at=iso(_NOW.replace(minute=minute)),
            tick_number=minute,
            decision=TickDecision.IDLE,
            note="",
            prompt_tokens=1,
            duration_seconds=0.1,
            max_entries=1000,  # no sweep pressure from the append calls themselves
            retention_days=365,
        )

    deleted = repository.sweep(
        _NOW.replace(minute=4), max_entries=2, retention_days=365
    )

    assert deleted == 3
    remaining = repository.list_recent(limit=10)
    assert sorted(e.tick_number for e in remaining) == [3, 4]


def test_sweep_on_an_empty_table_deletes_nothing(db: Database) -> None:
    assert HeartJournalRepository(db).sweep(_NOW, max_entries=10, retention_days=1) == 0


def test_append_sweeps_as_a_side_effect(db: Database) -> None:
    """`append()`'s own documented side effect: once the table exceeds `max_entries`,
    the oldest rows are gone without a separate `.sweep()` call.
    """
    repository = HeartJournalRepository(db)
    for minute in range(3):
        _append(
            repository,
            created_at=iso(_NOW.replace(minute=minute)),
            tick_number=minute,
            max_entries=2,
            retention_days=365,
        )

    remaining = repository.list_recent(limit=10)

    assert sorted(e.tick_number for e in remaining) == [1, 2]
