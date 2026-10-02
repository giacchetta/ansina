from __future__ import annotations

from datetime import UTC, datetime

from ansina.auth.clock import iso
from ansina.config.settings import HeartSettings
from ansina.heart.eval.fixtures import load_fixtures
from ansina.heart.journal import HeartJournalRepository
from ansina.heart.tick.decision import TickDecision
from ansina.heart.tick.snapshot import SnapshotItem, build_prompt
from ansina.heart.tick.sources.recent_journal import RecentJournalSource
from ansina.storage.database import Database

_NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def test_collect_returns_nothing_when_recent_entries_is_zero(db: Database) -> None:
    repository = HeartJournalRepository(db)
    repository.append(
        created_at=iso(_NOW),
        tick_number=1,
        decision=TickDecision.IDLE,
        note="",
        prompt_tokens=1,
        duration_seconds=0.1,
        max_entries=100,
        retention_days=30,
    )
    source = RecentJournalSource(repository, recent_entries=0)

    assert source.collect() == ()


def test_collect_returns_nothing_on_a_cold_database(db: Database) -> None:
    source = RecentJournalSource(HeartJournalRepository(db), recent_entries=5)

    assert source.collect() == ()


def test_collect_returns_exactly_one_item(db: Database) -> None:
    repository = HeartJournalRepository(db)
    repository.append(
        created_at=iso(_NOW),
        tick_number=1,
        decision=TickDecision.IDLE,
        note="",
        prompt_tokens=1,
        duration_seconds=0.1,
        max_entries=100,
        retention_days=30,
    )
    source = RecentJournalSource(repository, recent_entries=5)

    items = source.collect()

    assert len(items) == 1
    assert isinstance(items[0], SnapshotItem)
    assert items[0].source == "recent_journal"


def test_collect_item_priority_is_below_the_daemon_state_floor(db: Database) -> None:
    """`sources.daemon_state`'s own lowest band is `_PRIORITY_BACKGROUND = 0` — this
    source must sit below it, so recent history is always the first thing
    `build_prompt` trims under budget pressure.
    """
    repository = HeartJournalRepository(db)
    repository.append(
        created_at=iso(_NOW),
        tick_number=1,
        decision=TickDecision.IDLE,
        note="",
        prompt_tokens=1,
        duration_seconds=0.1,
        max_entries=100,
        retention_days=30,
    )
    source = RecentJournalSource(repository, recent_entries=5)

    assert source.collect()[0].priority < 0


def test_collect_replays_oldest_first(db: Database) -> None:
    repository = HeartJournalRepository(db)
    for minute, decision in enumerate(
        (TickDecision.IDLE, TickDecision.ACT, TickDecision.ESCALATE)
    ):
        repository.append(
            created_at=iso(_NOW.replace(minute=minute)),
            tick_number=minute,
            decision=decision,
            note="",
            prompt_tokens=1,
            duration_seconds=0.1,
            max_entries=100,
            retention_days=30,
        )
    source = RecentJournalSource(repository, recent_entries=5)

    text = source.collect()[0].text

    # `list_recent` returns newest-first (tick 2, 1, 0); the rendered line must read
    # oldest-first (tick 0, 1, 2) instead.
    assert text.index("tick 0") < text.index("tick 1") < text.index("tick 2")


def test_collect_truncates_each_rows_note_preview(db: Database) -> None:
    repository = HeartJournalRepository(db)
    repository.append(
        created_at=iso(_NOW),
        tick_number=1,
        decision=TickDecision.ACT,
        note="x" * 1000,
        prompt_tokens=1,
        duration_seconds=0.1,
        max_entries=100,
        retention_days=30,
    )
    source = RecentJournalSource(repository, recent_entries=5)

    text = source.collect()[0].text

    assert "x" * 1000 not in text
    assert "x" * 80 in text


def test_collect_respects_recent_entries_limit(db: Database) -> None:
    repository = HeartJournalRepository(db)
    for minute in range(10):
        repository.append(
            created_at=iso(_NOW.replace(minute=minute)),
            tick_number=minute,
            decision=TickDecision.IDLE,
            note="",
            prompt_tokens=1,
            duration_seconds=0.1,
            max_entries=100,
            retention_days=30,
        )
    source = RecentJournalSource(repository, recent_entries=3)

    text = source.collect()[0].text

    # Only the 3 most recent ticks (7, 8, 9) should appear.
    for tick in (7, 8, 9):
        assert f"tick {tick}" in text
    for tick in range(7):
        assert f"tick {tick}:" not in text


# --- the budget AC: proven against the real fixture set, not asserted --------------


def test_recent_journal_never_blows_the_token_budget_on_any_fixture(
    db: Database,
) -> None:
    """A worst-case `RecentJournalSource` item — every one of `recent_entries` rows
    present, each note at the replay preview's own length cap — added to every tick
    fixture's items, rendered through the real `build_prompt`, must never push the
    prompt over `context_tokens - max_output_tokens`. `token_count=len` (character
    count) is a conservative stand-in for a real tokenizer: real BPE tokenization of
    English prose never produces *more* tokens than characters, so clearing this
    bound under character-counting clears the real bound too.
    """
    heart_settings = HeartSettings()
    budget_tokens = heart_settings.context_tokens - heart_settings.max_output_tokens
    recent_entries = heart_settings.journal.recent_entries

    repository = HeartJournalRepository(db)
    for minute in range(recent_entries):
        repository.append(
            created_at=iso(_NOW.replace(minute=minute)),
            tick_number=999_999,
            decision=TickDecision.ESCALATE,
            note="x" * 1000,  # exceeds the replay preview's own cap either way
            prompt_tokens=1,
            duration_seconds=0.1,
            max_entries=10_000,
            retention_days=3650,
        )
    source = RecentJournalSource(repository, recent_entries=recent_entries)
    worst_case_items = source.collect()

    for fixture in load_fixtures():
        items = [*fixture.items, *worst_case_items]
        prompt = build_prompt(items, budget_tokens=budget_tokens, token_count=len)
        assert prompt.tokens <= budget_tokens, (
            f"fixture {fixture.id!r} plus a worst-case recent-journal item exceeded "
            f"the token budget: {prompt.tokens} > {budget_tokens}"
        )
