from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ansina.auth.clock import iso
from ansina.opencode.repository import OpencodeRunRepository
from ansina.storage.database import Database

_NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def test_create_returns_a_queued_row(db: Database) -> None:
    repository = OpencodeRunRepository(db)

    run = repository.create(created_at=iso(_NOW), prompt="say hello")

    assert run.id
    assert run.created_at == iso(_NOW)
    assert run.status == "queued"
    assert run.prompt == "say hello"
    assert run.started_at is None
    assert run.completed_at is None
    assert run.session_id is None
    assert run.model is None
    assert run.agent is None
    assert run.exit_code is None
    assert run.error_detail == ""
    assert run.event_count == 0
    assert run.tool_call_count == 0
    assert run.permission_denied_count == 0
    assert run.parse_error_count == 0
    assert run.prompt_tokens is None
    assert run.completion_tokens is None
    assert run.duration_seconds is None
    assert run.retry_of is None
    assert run.intervention_count == 0


def test_create_persists_optional_fields(db: Database) -> None:
    repository = OpencodeRunRepository(db)

    run = repository.create(
        created_at=iso(_NOW),
        prompt="continue",
        session_id="sess-1",
        model="claude",
        agent="build",
        retry_of="prior-run-id",
    )

    assert run.session_id == "sess-1"
    assert run.model == "claude"
    assert run.agent == "build"
    assert run.retry_of == "prior-run-id"


def test_create_generates_a_unique_id_per_row(db: Database) -> None:
    repository = OpencodeRunRepository(db)

    first = repository.create(created_at=iso(_NOW), prompt="a")
    second = repository.create(created_at=iso(_NOW), prompt="b")

    assert first.id != second.id


def test_get_returns_none_for_an_unknown_id(db: Database) -> None:
    assert OpencodeRunRepository(db).get("does-not-exist") is None


def test_get_returns_the_current_row(db: Database) -> None:
    repository = OpencodeRunRepository(db)
    created = repository.create(created_at=iso(_NOW), prompt="say hello")

    fetched = repository.get(created.id)

    assert fetched == created


def test_update_status_to_running_sets_started_at(db: Database) -> None:
    repository = OpencodeRunRepository(db)
    run = repository.create(created_at=iso(_NOW), prompt="say hello")

    updated = repository.update_status(
        run.id, "running", started_at=iso(_NOW.replace(minute=1))
    )

    assert updated.status == "running"
    assert updated.started_at == iso(_NOW.replace(minute=1))
    assert updated.completed_at is None


def test_update_status_leaves_unspecified_fields_untouched(db: Database) -> None:
    repository = OpencodeRunRepository(db)
    run = repository.create(created_at=iso(_NOW), prompt="say hello")
    repository.update_status(run.id, "running", started_at=iso(_NOW))

    # No `exit_code`/`error_detail`/counters passed — they must stay at their
    # current (default) values, not be clobbered to NULL/0 by the transition.
    updated = repository.update_status(
        run.id, "succeeded", completed_at=iso(_NOW.replace(minute=2))
    )

    assert updated.started_at == iso(_NOW)  # untouched by the second call
    assert updated.exit_code is None
    assert updated.event_count == 0
    assert updated.error_detail == ""


def test_update_status_to_terminal_persists_every_field(db: Database) -> None:
    repository = OpencodeRunRepository(db)
    run = repository.create(created_at=iso(_NOW), prompt="say hello")
    repository.update_status(run.id, "running", started_at=iso(_NOW))

    updated = repository.update_status(
        run.id,
        "succeeded",
        completed_at=iso(_NOW.replace(minute=3)),
        exit_code=0,
        error_detail="",
        event_count=5,
        tool_call_count=2,
        permission_denied_count=1,
        parse_error_count=0,
        prompt_tokens=100,
        completion_tokens=50,
        duration_seconds=12.5,
        intervention_count=1,
    )

    assert updated.status == "succeeded"
    assert updated.completed_at == iso(_NOW.replace(minute=3))
    assert updated.exit_code == 0
    assert updated.event_count == 5
    assert updated.tool_call_count == 2
    assert updated.permission_denied_count == 1
    assert updated.prompt_tokens == 100
    assert updated.completion_tokens == 50
    assert updated.duration_seconds == 12.5
    assert updated.intervention_count == 1


@pytest.mark.parametrize(
    "terminal_status", ["succeeded", "failed", "timeout", "cancelled"]
)
def test_update_status_refuses_a_transition_out_of_a_terminal_status(
    db: Database, terminal_status: str
) -> None:
    """The one mutator's own invariant — a zero-rowcount update is an internal bug,
    not a client-facing condition, so it asserts rather than raising a typed error.
    """
    repository = OpencodeRunRepository(db)
    run = repository.create(created_at=iso(_NOW), prompt="say hello")
    repository.update_status(run.id, terminal_status, completed_at=iso(_NOW))

    with pytest.raises(AssertionError):
        repository.update_status(run.id, "running")


def test_update_status_on_an_unknown_id_raises(db: Database) -> None:
    with pytest.raises(AssertionError):
        OpencodeRunRepository(db).update_status("does-not-exist", "running")


def test_list_recent_orders_newest_first(db: Database) -> None:
    repository = OpencodeRunRepository(db)
    t1 = iso(_NOW)
    t2 = iso(_NOW.replace(minute=1))
    t3 = iso(_NOW.replace(minute=2))
    repository.create(created_at=t1, prompt="1")
    repository.create(created_at=t2, prompt="2")
    repository.create(created_at=t3, prompt="3")

    runs = repository.list_recent(limit=10)

    assert [r.prompt for r in runs] == ["3", "2", "1"]


def test_list_recent_respects_limit(db: Database) -> None:
    repository = OpencodeRunRepository(db)
    for minute in range(5):
        repository.create(created_at=iso(_NOW.replace(minute=minute)), prompt="p")

    runs = repository.list_recent(limit=2)

    assert len(runs) == 2


def test_list_recent_filters_by_since(db: Database) -> None:
    repository = OpencodeRunRepository(db)
    old = iso(_NOW)
    new = iso(_NOW.replace(minute=5))
    repository.create(created_at=old, prompt="old")
    repository.create(created_at=new, prompt="new")

    runs = repository.list_recent(limit=10, since=old)

    assert [r.prompt for r in runs] == ["new"]


def test_list_recent_on_an_empty_table_returns_an_empty_list(db: Database) -> None:
    assert OpencodeRunRepository(db).list_recent(limit=10) == []


def test_count_active_counts_only_queued_and_running(db: Database) -> None:
    repository = OpencodeRunRepository(db)
    queued = repository.create(created_at=iso(_NOW), prompt="queued")
    running = repository.create(created_at=iso(_NOW), prompt="running")
    repository.update_status(running.id, "running", started_at=iso(_NOW))
    succeeded = repository.create(created_at=iso(_NOW), prompt="succeeded")
    repository.update_status(succeeded.id, "running", started_at=iso(_NOW))
    repository.update_status(succeeded.id, "succeeded", completed_at=iso(_NOW))

    assert repository.count_active() == 2
    assert queued.status == "queued"  # sanity: never mutated above


def test_count_active_on_an_empty_table_is_zero(db: Database) -> None:
    assert OpencodeRunRepository(db).count_active() == 0


def test_sweep_deletes_rows_older_than_retention_days(db: Database) -> None:
    repository = OpencodeRunRepository(db)
    old = iso(_NOW)
    new = iso(_NOW.replace(day=10))
    repository.create(created_at=old, prompt="old")
    repository.create(created_at=new, prompt="new")

    deleted = repository.sweep(_NOW.replace(day=10), max_entries=1000, retention_days=5)

    assert deleted == 1
    remaining = repository.list_recent(limit=10)
    assert [r.prompt for r in remaining] == ["new"]


def test_sweep_keeps_only_the_newest_max_entries(db: Database) -> None:
    repository = OpencodeRunRepository(db)
    for minute in range(5):
        repository.create(
            created_at=iso(_NOW.replace(minute=minute)), prompt=str(minute)
        )

    deleted = repository.sweep(
        _NOW.replace(minute=4), max_entries=2, retention_days=365
    )

    assert deleted == 3
    remaining = repository.list_recent(limit=10)
    assert sorted(r.prompt for r in remaining) == ["3", "4"]


def test_sweep_on_an_empty_table_deletes_nothing(db: Database) -> None:
    assert OpencodeRunRepository(db).sweep(_NOW, max_entries=10, retention_days=1) == 0


def test_sweep_stale_on_boot_fails_queued_and_running_rows(db: Database) -> None:
    repository = OpencodeRunRepository(db)
    queued = repository.create(created_at=iso(_NOW), prompt="queued")
    running = repository.create(created_at=iso(_NOW), prompt="running")
    repository.update_status(running.id, "running", started_at=iso(_NOW))
    succeeded = repository.create(created_at=iso(_NOW), prompt="succeeded")
    repository.update_status(succeeded.id, "running", started_at=iso(_NOW))
    repository.update_status(succeeded.id, "succeeded", completed_at=iso(_NOW))

    swept = repository.sweep_stale_on_boot(_NOW.replace(minute=5))

    assert swept == 2
    reloaded_queued = repository.get(queued.id)
    reloaded_running = repository.get(running.id)
    reloaded_succeeded = repository.get(succeeded.id)
    assert reloaded_queued is not None
    assert reloaded_queued.status == "failed"
    assert reloaded_queued.error_detail == "daemon restarted"
    assert reloaded_queued.completed_at == iso(_NOW.replace(minute=5))
    assert reloaded_running is not None
    assert reloaded_running.status == "failed"
    assert reloaded_running.error_detail == "daemon restarted"
    assert reloaded_succeeded is not None
    assert reloaded_succeeded.status == "succeeded"  # untouched — already terminal


def test_sweep_stale_on_boot_on_an_empty_table_sweeps_nothing(db: Database) -> None:
    assert OpencodeRunRepository(db).sweep_stale_on_boot(_NOW) == 0


# --- issue #78: the three `OpencodeStateSource` read methods -----------------------


def test_counts_by_status_splits_queued_and_running(db: Database) -> None:
    repository = OpencodeRunRepository(db)
    repository.create(created_at=iso(_NOW), prompt="queued-1")
    repository.create(created_at=iso(_NOW), prompt="queued-2")
    running = repository.create(created_at=iso(_NOW), prompt="running")
    repository.update_status(running.id, "running", started_at=iso(_NOW))
    succeeded = repository.create(created_at=iso(_NOW), prompt="succeeded")
    repository.update_status(succeeded.id, "running", started_at=iso(_NOW))
    repository.update_status(succeeded.id, "succeeded", completed_at=iso(_NOW))

    counts = repository.counts_by_status()

    assert counts == {"queued": 2, "running": 1}


def test_counts_by_status_on_an_empty_table_returns_an_empty_mapping(
    db: Database,
) -> None:
    assert OpencodeRunRepository(db).counts_by_status() == {}


def test_counts_by_status_omits_a_status_with_zero_rows(db: Database) -> None:
    repository = OpencodeRunRepository(db)
    repository.create(created_at=iso(_NOW), prompt="queued-only")

    counts = repository.counts_by_status()

    assert counts == {"queued": 1}
    assert "running" not in counts


def test_list_running_returns_only_running_rows(db: Database) -> None:
    repository = OpencodeRunRepository(db)
    repository.create(created_at=iso(_NOW), prompt="queued")
    running = repository.create(created_at=iso(_NOW), prompt="running")
    repository.update_status(running.id, "running", started_at=iso(_NOW))
    succeeded = repository.create(created_at=iso(_NOW), prompt="succeeded")
    repository.update_status(succeeded.id, "running", started_at=iso(_NOW))
    repository.update_status(succeeded.id, "succeeded", completed_at=iso(_NOW))

    rows = repository.list_running()

    assert [r.id for r in rows] == [running.id]


def test_list_running_on_an_empty_table_returns_an_empty_list(db: Database) -> None:
    assert OpencodeRunRepository(db).list_running() == []


def test_recent_outcome_counts_sums_failed_timeout_and_permission_denied(
    db: Database,
) -> None:
    repository = OpencodeRunRepository(db)
    failed = repository.create(created_at=iso(_NOW), prompt="failed")
    repository.update_status(failed.id, "running", started_at=iso(_NOW))
    repository.update_status(
        failed.id,
        "failed",
        completed_at=iso(_NOW),
        error_detail="boom",
        permission_denied_count=2,
    )
    timed_out = repository.create(created_at=iso(_NOW), prompt="timeout")
    repository.update_status(timed_out.id, "running", started_at=iso(_NOW))
    repository.update_status(
        timed_out.id, "timeout", completed_at=iso(_NOW), permission_denied_count=1
    )
    succeeded = repository.create(created_at=iso(_NOW), prompt="succeeded")
    repository.update_status(succeeded.id, "running", started_at=iso(_NOW))
    repository.update_status(
        succeeded.id, "succeeded", completed_at=iso(_NOW), permission_denied_count=5
    )

    since = iso(_NOW.replace(year=_NOW.year - 1))
    failed_count, timeout_count, permission_denied_total = (
        repository.recent_outcome_counts(since)
    )

    assert failed_count == 1
    assert timeout_count == 1
    # Summed across *every* recent row regardless of status, not just failed/timeout.
    assert permission_denied_total == 2 + 1 + 5


def test_recent_outcome_counts_excludes_rows_at_or_before_since(db: Database) -> None:
    repository = OpencodeRunRepository(db)
    old = repository.create(created_at=iso(_NOW), prompt="old-failure")
    repository.update_status(old.id, "running", started_at=iso(_NOW))
    repository.update_status(old.id, "failed", completed_at=iso(_NOW))

    counts = repository.recent_outcome_counts(iso(_NOW))

    assert counts == (0, 0, 0)


def test_recent_outcome_counts_on_an_empty_table_is_all_zero(db: Database) -> None:
    assert OpencodeRunRepository(db).recent_outcome_counts(iso(_NOW)) == (0, 0, 0)
