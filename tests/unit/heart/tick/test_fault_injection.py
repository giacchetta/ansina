from __future__ import annotations

from pathlib import Path

import pytest

from ansina.heart.tick.decision import TickDecision
from ansina.heart.tick.fault_injection import (
    FaultInjectionError,
    FaultInjector,
    FaultKind,
    FaultScheduleError,
    FaultWindow,
    load_fault_schedule,
)
from ansina.heart.tick.snapshot import TickPrompt

_PROMPT = TickPrompt(text="x", tokens=1, items_included=0, items_dropped=0)


class _FakeTick:
    def __init__(self, ticks_run: int) -> None:
        self.ticks_run = ticks_run


# --- FaultWindow ---------------------------------------------------------------------


def test_fault_window_covers_its_own_tick_range() -> None:
    window = FaultWindow(
        id="w1",
        fault=FaultKind.TICK_FAILURE,
        at_tick=5,
        duration_ticks=3,
        expected_decision=TickDecision.ACT,
    )

    assert window.end_tick == 7
    assert window.covers(4) is False
    assert window.covers(5) is True
    assert window.covers(6) is True
    assert window.covers(7) is True
    assert window.covers(8) is False


# --- load_fault_schedule: happy path --------------------------------------------------


def test_load_fault_schedule_parses_every_field(tmp_path: Path) -> None:
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(
        """
        [
            {
                "id": "esc-01",
                "fault": "tick_overrun",
                "at_tick": 10,
                "duration_ticks": 3,
                "expected_decision": "escalate",
                "note": "a possible stuck backend",
                "sleep_seconds": 25.0
            },
            {
                "id": "act-01",
                "fault": "tick_failure",
                "at_tick": 1,
                "duration_ticks": 1,
                "expected_decision": "act"
            }
        ]
        """,
        encoding="utf-8",
    )

    schedule = load_fault_schedule(schedule_path)

    assert [window.id for window in schedule] == ["act-01", "esc-01"]
    first = schedule[0]
    assert first.fault is FaultKind.TICK_FAILURE
    assert first.at_tick == 1
    assert first.duration_ticks == 1
    assert first.expected_decision is TickDecision.ACT
    assert first.note == ""
    assert first.sleep_seconds == 0.0
    second = schedule[1]
    assert second.note == "a possible stuck backend"
    assert second.sleep_seconds == 25.0


def test_load_fault_schedule_accepts_a_str_path(tmp_path: Path) -> None:
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text("[]", encoding="utf-8")

    assert load_fault_schedule(str(schedule_path)) == ()


def test_load_fault_schedule_allows_different_kind_windows_to_overlap(
    tmp_path: Path,
) -> None:
    """Recreates `tick_fixtures.jsonl`'s own "two simultaneous axes" scenarios
    (e.g. `self-esc-01`/`self-esc-03`) — different `FaultKind`s may cover the same
    tick on purpose.
    """
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(
        """
        [
            {"id": "a", "fault": "readiness_check", "at_tick": 1,
             "duration_ticks": 5, "expected_decision": "escalate"},
            {"id": "b", "fault": "tick_failure", "at_tick": 3,
             "duration_ticks": 2, "expected_decision": "escalate"}
        ]
        """,
        encoding="utf-8",
    )

    schedule = load_fault_schedule(schedule_path)

    assert len(schedule) == 2


# --- load_fault_schedule: validation failures ----------------------------------------


def test_load_fault_schedule_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FaultScheduleError, match="could not be read"):
        load_fault_schedule(tmp_path / "missing.json")


def test_load_fault_schedule_invalid_json_raises(tmp_path: Path) -> None:
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text("not json", encoding="utf-8")

    with pytest.raises(FaultScheduleError, match="invalid JSON"):
        load_fault_schedule(schedule_path)


def test_load_fault_schedule_non_list_raises(tmp_path: Path) -> None:
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text("{}", encoding="utf-8")

    with pytest.raises(FaultScheduleError, match="must be a JSON array"):
        load_fault_schedule(schedule_path)


def test_load_fault_schedule_non_dict_entry_raises(tmp_path: Path) -> None:
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text('["not-an-object"]', encoding="utf-8")

    with pytest.raises(FaultScheduleError, match="must be a JSON object"):
        load_fault_schedule(schedule_path)


def test_load_fault_schedule_missing_field_raises(tmp_path: Path) -> None:
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(
        '[{"fault": "tick_failure", "at_tick": 1, "duration_ticks": 1, '
        '"expected_decision": "act"}]',
        encoding="utf-8",
    )

    with pytest.raises(FaultScheduleError, match="missing field"):
        load_fault_schedule(schedule_path)


def test_load_fault_schedule_invalid_fault_kind_raises(tmp_path: Path) -> None:
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(
        '[{"id": "x", "fault": "not_a_kind", "at_tick": 1, "duration_ticks": 1, '
        '"expected_decision": "act"}]',
        encoding="utf-8",
    )

    with pytest.raises(FaultScheduleError):
        load_fault_schedule(schedule_path)


@pytest.mark.parametrize("at_tick", [0, -1])
def test_load_fault_schedule_at_tick_must_be_positive(
    tmp_path: Path, at_tick: int
) -> None:
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(
        f'[{{"id": "x", "fault": "tick_failure", "at_tick": {at_tick}, '
        '"duration_ticks": 1, "expected_decision": "act"}]',
        encoding="utf-8",
    )

    with pytest.raises(FaultScheduleError, match="at_tick must be >= 1"):
        load_fault_schedule(schedule_path)


@pytest.mark.parametrize("duration_ticks", [0, -1])
def test_load_fault_schedule_duration_ticks_must_be_positive(
    tmp_path: Path, duration_ticks: int
) -> None:
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(
        f'[{{"id": "x", "fault": "tick_failure", "at_tick": 1, '
        f'"duration_ticks": {duration_ticks}, "expected_decision": "act"}}]',
        encoding="utf-8",
    )

    with pytest.raises(FaultScheduleError, match="duration_ticks must be >= 1"):
        load_fault_schedule(schedule_path)


def test_load_fault_schedule_overrun_without_sleep_seconds_rejected(
    tmp_path: Path,
) -> None:
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(
        '[{"id": "x", "fault": "tick_overrun", "at_tick": 1, "duration_ticks": 1, '
        '"expected_decision": "escalate"}]',
        encoding="utf-8",
    )

    with pytest.raises(FaultScheduleError, match="sleep_seconds must be > 0"):
        load_fault_schedule(schedule_path)


def test_load_fault_schedule_non_overrun_with_sleep_seconds_rejected(
    tmp_path: Path,
) -> None:
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(
        '[{"id": "x", "fault": "tick_failure", "at_tick": 1, "duration_ticks": 1, '
        '"expected_decision": "act", "sleep_seconds": 5.0}]',
        encoding="utf-8",
    )

    with pytest.raises(FaultScheduleError, match="only meaningful for"):
        load_fault_schedule(schedule_path)


def test_load_fault_schedule_duplicate_ids_rejected(tmp_path: Path) -> None:
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(
        """
        [
            {"id": "x", "fault": "tick_failure", "at_tick": 1,
             "duration_ticks": 1, "expected_decision": "act"},
            {"id": "x", "fault": "tick_failure", "at_tick": 5,
             "duration_ticks": 1, "expected_decision": "act"}
        ]
        """,
        encoding="utf-8",
    )

    with pytest.raises(FaultScheduleError, match="duplicate window id"):
        load_fault_schedule(schedule_path)


def test_load_fault_schedule_same_kind_overlap_rejected(tmp_path: Path) -> None:
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(
        """
        [
            {"id": "a", "fault": "tick_failure", "at_tick": 1,
             "duration_ticks": 5, "expected_decision": "act"},
            {"id": "b", "fault": "tick_failure", "at_tick": 3,
             "duration_ticks": 2, "expected_decision": "escalate"}
        ]
        """,
        encoding="utf-8",
    )

    with pytest.raises(FaultScheduleError, match="overlap at tick"):
        load_fault_schedule(schedule_path)


# --- FaultInjector: construction / bind ----------------------------------------------


def test_fault_injector_requires_bind_before_collect() -> None:
    injector = FaultInjector(())

    with pytest.raises(RuntimeError, match="bind"):
        injector.collect()


def test_fault_injector_requires_bind_before_readiness_ok() -> None:
    injector = FaultInjector(())

    with pytest.raises(RuntimeError, match="bind"):
        injector.readiness_ok()


def test_fault_injector_exposes_its_schedule() -> None:
    window = FaultWindow(
        id="x",
        fault=FaultKind.TICK_FAILURE,
        at_tick=1,
        duration_ticks=1,
        expected_decision=TickDecision.ACT,
    )
    injector = FaultInjector((window,))

    assert injector.schedule == (window,)


# --- FaultInjector: collect() (TICK_OVERRUN role) -------------------------------------


def test_fault_injector_collect_sleeps_during_an_active_overrun_window() -> None:
    sleeps: list[float] = []
    window = FaultWindow(
        id="slow",
        fault=FaultKind.TICK_OVERRUN,
        at_tick=2,
        duration_ticks=2,
        expected_decision=TickDecision.ESCALATE,
        sleep_seconds=12.5,
    )
    injector = FaultInjector((window,), sleep=sleeps.append)
    injector.bind(_FakeTick(ticks_run=1))  # upcoming tick = 2, inside [2, 3]

    items = injector.collect()

    assert items == ()
    assert sleeps == [12.5]


def test_fault_injector_collect_does_not_sleep_outside_an_overrun_window() -> None:
    sleeps: list[float] = []
    window = FaultWindow(
        id="slow",
        fault=FaultKind.TICK_OVERRUN,
        at_tick=2,
        duration_ticks=2,
        expected_decision=TickDecision.ESCALATE,
        sleep_seconds=12.5,
    )
    injector = FaultInjector((window,), sleep=sleeps.append)
    injector.bind(_FakeTick(ticks_run=10))  # upcoming tick = 11, outside [2, 3]

    items = injector.collect()

    assert items == ()
    assert sleeps == []


# --- FaultInjector: readiness_ok() (READINESS_CHECK role) -----------------------------


def test_fault_injector_readiness_ok_true_with_no_active_window() -> None:
    injector = FaultInjector(())
    injector.bind(_FakeTick(ticks_run=0))

    assert injector.readiness_ok() is True


def test_fault_injector_readiness_ok_false_during_an_active_window() -> None:
    window = FaultWindow(
        id="stuck-sensor",
        fault=FaultKind.READINESS_CHECK,
        at_tick=1,
        duration_ticks=3,
        expected_decision=TickDecision.ESCALATE,
    )
    injector = FaultInjector((window,))
    injector.bind(_FakeTick(ticks_run=1))  # upcoming tick = 2, inside [1, 3]

    assert injector.readiness_ok() is False


# --- FaultInjector: handle() (TICK_FAILURE role / DecisionHandler) --------------------


async def test_fault_injector_handle_raises_during_an_active_failure_window() -> None:
    window = FaultWindow(
        id="recurring-failure",
        fault=FaultKind.TICK_FAILURE,
        at_tick=5,
        duration_ticks=2,
        expected_decision=TickDecision.ESCALATE,
    )
    injector = FaultInjector((window,))

    with pytest.raises(FaultInjectionError, match="recurring-failure"):
        await injector.handle(
            TickDecision.IDLE, _PROMPT, tick_number=6, duration_seconds=0.1
        )


async def test_fault_injector_handle_is_a_noop_outside_a_failure_window() -> None:
    window = FaultWindow(
        id="recurring-failure",
        fault=FaultKind.TICK_FAILURE,
        at_tick=5,
        duration_ticks=2,
        expected_decision=TickDecision.ESCALATE,
    )
    injector = FaultInjector((window,))

    # No exception, no bind() needed — handle() uses the exact `tick_number` param,
    # never TickCounter.ticks_run.
    await injector.handle(
        TickDecision.IDLE, _PROMPT, tick_number=1, duration_seconds=0.1
    )


async def test_fault_injector_handle_ignores_non_failure_windows() -> None:
    window = FaultWindow(
        id="slow",
        fault=FaultKind.TICK_OVERRUN,
        at_tick=1,
        duration_ticks=5,
        expected_decision=TickDecision.ESCALATE,
        sleep_seconds=1.0,
    )
    injector = FaultInjector((window,))

    await injector.handle(
        TickDecision.IDLE, _PROMPT, tick_number=2, duration_seconds=0.1
    )
