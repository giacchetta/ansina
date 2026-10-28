from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ansina.config.settings import OpencodeSettings, Settings, TelemetrySettings
from ansina.errors import (
    OpencodeBusyError,
    OpencodeIntakePausedError,
    OpencodeRunFailedError,
)
from ansina.opencode.events import (
    RunEvent,
    RunEventType,
    RunOutcome,
    RunRequest,
    RunStatus,
)
from ansina.opencode.repository import OpencodeRunRepository
from ansina.opencode.service import OpencodeRunService, build_opencode_run_service
from ansina.opencode.telemetry import OpencodeTelemetryWriter
from ansina.storage.database import Database
from ansina.storage.migrator import run_migrations
from ansina.telemetry.rotation import SpooledRotatingWriter

if TYPE_CHECKING:
    from ansina.opencode.runner import OnEvent


def _opencode_settings(**kwargs: object) -> OpencodeSettings:
    kwargs.setdefault("enabled", True)
    return OpencodeSettings(**kwargs)  # type: ignore[arg-type]


def _telemetry_writer(tmp_path: Path) -> OpencodeTelemetryWriter:
    writer = SpooledRotatingWriter(
        spool_dir=tmp_path,
        file_prefix="opencode",
        max_file_bytes=10_000_000,
        max_spool_bytes=10_000_000,
        retention_hours=24.0,
    )
    return OpencodeTelemetryWriter(writer=writer, max_line_bytes=65_536)


def _read_lines(tmp_path: Path) -> list[dict[str, object]]:
    active = tmp_path / "opencode.jsonl"
    if not active.exists():
        return []
    return [json.loads(line) for line in active.read_text().splitlines()]


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"condition never became true within {timeout}s")


class _FakeAgentRunner:
    """A scripted `AgentRunner` — emits a fixed event sequence, then returns a
    fixed `RunOutcome`, or hangs/ignores cancellation, depending on construction.
    """

    def __init__(
        self,
        *,
        events: list[RunEvent] | None = None,
        outcome: RunOutcome | None = None,
    ) -> None:
        self.events = events or []
        self.outcome = outcome or RunOutcome(
            status=RunStatus.COMPLETED,
            exit_code=0,
            events_emitted=len(self.events),
            events_skipped=0,
            duration_seconds=0.1,
        )
        self.calls: list[RunRequest] = []

    async def run(self, request: RunRequest, on_event: OnEvent) -> RunOutcome:
        self.calls.append(request)
        for event in self.events:
            on_event(event)
        return self.outcome


class _HangingAgentRunner:
    """Never returns on its own — `run()` blocks on an `asyncio.Event` that only
    a real cancellation (letting `CancelledError` propagate through `await`)
    interrupts. Used to exercise `OpencodeRunService.cancel()`/`stop()` against a
    genuinely in-flight run.
    """

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self, request: RunRequest, on_event: OnEvent) -> RunOutcome:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable — only ever cancelled, never finishes")


class _RunnerThatEmitsThenHangs:
    """Emits one event (so `on_event` fires at least once), then hangs the same
    way `_HangingAgentRunner` does — lets a test observe `last_event_at` while the
    run is still genuinely `running`, before it's ever cancelled/completed.
    """

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self, request: RunRequest, on_event: OnEvent) -> RunOutcome:
        on_event(
            RunEvent(type=RunEventType.OTHER, raw_type="x", payload="{}", sequence=0)
        )
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable — only ever cancelled, never finishes")


class _UncancellableAgentRunner:
    """Ignores exactly one cancellation — enough to still be pending when
    `stop()`'s own bounded `asyncio.wait(..., timeout=...)` gives up (exercising
    its "still pending after shutdown_timeout_seconds" warning branch, the one
    path a well-behaved runner never takes), but finishes shortly afterward on
    its own rather than running forever — an immortal task would otherwise hang
    pytest-asyncio's own per-test event-loop teardown, which cancels and awaits
    every still-pending task before closing the loop.
    """

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self, request: RunRequest, on_event: OnEvent) -> RunOutcome:
        self.started.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(1000)
        # Deliberately slower than `test_stop_warns_...`'s own tiny
        # `shutdown_timeout_seconds` — `stop()` cancels exactly once, so this
        # uninterrupted delay is what keeps the task genuinely *pending* at the
        # moment `asyncio.wait(..., timeout=...)` gives up, rather than racing to
        # finish before it.
        await asyncio.sleep(0.2)
        return RunOutcome(
            status=RunStatus.COMPLETED,
            exit_code=0,
            events_emitted=0,
            events_skipped=0,
            duration_seconds=0.01,
        )


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "ansina.db")
    database.connect()
    run_migrations(database)
    return database


def _service(
    db: Database,
    tmp_path: Path,
    *,
    runner: object,
    settings: OpencodeSettings | None = None,
) -> tuple[OpencodeRunService, OpencodeRunRepository]:
    repository = OpencodeRunRepository(db)
    telemetry = _telemetry_writer(tmp_path)
    service = OpencodeRunService(
        runner=runner,  # type: ignore[arg-type]
        repository=repository,
        telemetry=telemetry,
        settings=settings or _opencode_settings(),
    )
    return service, repository


# --- submit(): admission control, before any row is ever created -------------------


async def test_submit_creates_a_queued_row_and_writes_submitted_telemetry(
    db: Database, tmp_path: Path
) -> None:
    runner = _HangingAgentRunner()
    service, repository = _service(db, tmp_path, runner=runner)

    run = await service.submit(RunRequest(prompt="say hello"))

    assert run.status == "queued"
    assert run.prompt == "say hello"
    assert repository.get(run.id) == run
    lines = _read_lines(tmp_path)
    assert lines[0]["event"] == "submitted"
    assert lines[0]["run_id"] == run.id
    await service.stop()


async def test_submit_refuses_when_intake_is_paused(
    db: Database, tmp_path: Path
) -> None:
    service, repository = _service(db, tmp_path, runner=_FakeAgentRunner())
    service.pause_intake()

    with pytest.raises(OpencodeIntakePausedError):
        await service.submit(RunRequest(prompt="say hello"))

    assert repository.list_recent(limit=10) == []
    assert _read_lines(tmp_path) == []


async def test_submit_refuses_an_oversized_prompt(db: Database, tmp_path: Path) -> None:
    settings = _opencode_settings(max_prompt_chars=5)
    service, repository = _service(
        db, tmp_path, runner=_FakeAgentRunner(), settings=settings
    )

    with pytest.raises(OpencodeRunFailedError):
        await service.submit(RunRequest(prompt="way too long"))

    assert repository.list_recent(limit=10) == []
    assert _read_lines(tmp_path) == []


async def test_submit_refuses_when_backlog_is_full(
    db: Database, tmp_path: Path
) -> None:
    settings = _opencode_settings(queue_max_depth=1, max_concurrent_runs=1)
    runner = _HangingAgentRunner()
    service, repository = _service(db, tmp_path, runner=runner, settings=settings)

    first = await service.submit(RunRequest(prompt="first"))
    with pytest.raises(OpencodeBusyError):
        await service.submit(RunRequest(prompt="second"))

    assert [r.id for r in repository.list_recent(limit=10)] == [first.id]
    await service.stop()


async def test_submit_does_not_persist_a_row_for_a_refused_combination(
    db: Database, tmp_path: Path
) -> None:
    """`RunRequest.__post_init__`'s own `ValueError` (session_id + continue_last)
    is the route layer's job to catch (`api/routes/opencode_runs.py`) — this test
    only pins that the error itself still surfaces from the dataclass, not that
    `submit()` catches it (it doesn't; the route does).
    """
    with pytest.raises(ValueError, match="mutually exclusive"):
        RunRequest(prompt="x", session_id="s", continue_last=True)


# --- the full run lifecycle ----------------------------------------------------------


async def test_a_completed_run_transitions_through_running_to_succeeded(
    db: Database, tmp_path: Path
) -> None:
    event = RunEvent(
        type=RunEventType.OTHER, raw_type="tool_call", payload="{}", sequence=0
    )
    outcome = RunOutcome(
        status=RunStatus.COMPLETED,
        exit_code=0,
        events_emitted=1,
        events_skipped=2,
        duration_seconds=1.5,
    )
    runner = _FakeAgentRunner(events=[event], outcome=outcome)
    service, repository = _service(db, tmp_path, runner=runner)

    run = await service.submit(RunRequest(prompt="say hello"))
    await _wait_until(lambda: repository.get(run.id).status == "succeeded")  # type: ignore[union-attr]

    final = repository.get(run.id)
    assert final is not None
    assert final.status == "succeeded"
    assert final.exit_code == 0
    assert final.error_detail == ""
    assert final.event_count == 1
    assert final.parse_error_count == 2
    assert final.duration_seconds == 1.5
    assert final.started_at is not None
    assert final.completed_at is not None

    events = [line["event"] for line in _read_lines(tmp_path)]
    assert events == ["submitted", "started", "run_event", "completed"]


async def test_a_failed_run_persists_the_error_detail(
    db: Database, tmp_path: Path
) -> None:
    outcome = RunOutcome(
        status=RunStatus.FAILED,
        exit_code=1,
        events_emitted=0,
        events_skipped=0,
        duration_seconds=0.2,
        error="opencode exited with code 1",
    )
    service, repository = _service(
        db, tmp_path, runner=_FakeAgentRunner(outcome=outcome)
    )

    run = await service.submit(RunRequest(prompt="say hello"))
    await _wait_until(lambda: repository.get(run.id).status == "failed")  # type: ignore[union-attr]

    final = repository.get(run.id)
    assert final is not None
    assert final.exit_code == 1
    assert final.error_detail == "opencode exited with code 1"
    assert _read_lines(tmp_path)[-1]["event"] == "failed"


async def test_a_timed_out_run_persists_timeout_status(
    db: Database, tmp_path: Path
) -> None:
    outcome = RunOutcome(
        status=RunStatus.TIMEOUT,
        exit_code=None,
        events_emitted=0,
        events_skipped=0,
        duration_seconds=900.0,
        error="run exceeded opencode.run_timeout_seconds=900.0",
    )
    service, repository = _service(
        db, tmp_path, runner=_FakeAgentRunner(outcome=outcome)
    )

    run = await service.submit(RunRequest(prompt="say hello"))
    await _wait_until(lambda: repository.get(run.id).status == "timeout")  # type: ignore[union-attr]

    assert _read_lines(tmp_path)[-1]["event"] == "timeout"


# --- cancellation ---------------------------------------------------------------------


async def test_cancel_an_inflight_run_marks_it_cancelled(
    db: Database, tmp_path: Path
) -> None:
    runner = _HangingAgentRunner()
    service, repository = _service(db, tmp_path, runner=runner)

    run = await service.submit(RunRequest(prompt="say hello"))
    await asyncio.wait_for(runner.started.wait(), timeout=2.0)

    cancelled = await service.cancel(run.id)

    assert cancelled is True
    final = repository.get(run.id)
    assert final is not None
    assert final.status == "cancelled"
    assert _read_lines(tmp_path)[-1]["event"] == "cancelled"


async def test_cancel_returns_false_for_an_unknown_run_id(
    db: Database, tmp_path: Path
) -> None:
    service, _repository = _service(db, tmp_path, runner=_FakeAgentRunner())

    assert await service.cancel("does-not-exist") is False


async def test_cancel_returns_false_for_an_already_terminal_run(
    db: Database, tmp_path: Path
) -> None:
    service, repository = _service(db, tmp_path, runner=_FakeAgentRunner())

    run = await service.submit(RunRequest(prompt="say hello"))
    await _wait_until(lambda: repository.get(run.id).status == "succeeded")  # type: ignore[union-attr]
    # Let the done-callback actually pop the task from the service's own tracking
    # dict — it runs on the next loop iteration after the task finishes, not
    # synchronously at that moment.
    await asyncio.sleep(0)

    assert await service.cancel(run.id) is False


# --- the intake kill switch -----------------------------------------------------------


def test_intake_starts_unpaused_by_default(db: Database, tmp_path: Path) -> None:
    service, _repository = _service(db, tmp_path, runner=_FakeAgentRunner())

    assert service.intake_paused is False


def test_pause_intake_sets_the_flag(db: Database, tmp_path: Path) -> None:
    service, _repository = _service(db, tmp_path, runner=_FakeAgentRunner())

    service.pause_intake()

    assert service.intake_paused is True


def test_resume_intake_clears_the_flag(db: Database, tmp_path: Path) -> None:
    service, _repository = _service(db, tmp_path, runner=_FakeAgentRunner())
    service.pause_intake()

    service.resume_intake()

    assert service.intake_paused is False


def test_intake_paused_on_boot_seeds_the_initial_state(
    db: Database, tmp_path: Path
) -> None:
    settings = _opencode_settings(intake_paused_on_boot=True)
    service, _repository = _service(
        db, tmp_path, runner=_FakeAgentRunner(), settings=settings
    )

    assert service.intake_paused is True


def test_is_healthy_is_always_true(db: Database, tmp_path: Path) -> None:
    service, _repository = _service(db, tmp_path, runner=_FakeAgentRunner())

    assert service.is_healthy() is True


def test_repository_property_exposes_the_same_repository(
    db: Database, tmp_path: Path
) -> None:
    service, repository = _service(db, tmp_path, runner=_FakeAgentRunner())

    assert service.repository is repository


# --- stop(): lifespan shutdown --------------------------------------------------------


async def test_stop_cancels_every_inflight_run(db: Database, tmp_path: Path) -> None:
    runner = _HangingAgentRunner()
    service, repository = _service(db, tmp_path, runner=runner)

    run = await service.submit(RunRequest(prompt="say hello"))
    await asyncio.wait_for(runner.started.wait(), timeout=2.0)

    await service.stop()

    final = repository.get(run.id)
    assert final is not None
    assert final.status == "cancelled"


async def test_stop_warns_and_returns_when_a_task_outlives_the_timeout(
    db: Database, tmp_path: Path
) -> None:
    settings = _opencode_settings(shutdown_timeout_seconds=0.05)
    runner = _UncancellableAgentRunner()
    service, _repository = _service(db, tmp_path, runner=runner, settings=settings)

    await service.submit(RunRequest(prompt="say hello"))
    await asyncio.wait_for(runner.started.wait(), timeout=2.0)

    await service.stop()  # must return within shutdown_timeout_seconds, not hang

    # Let the runner's own un-cancellable tail delay actually finish before this
    # test ends, so nothing is still pending when pytest-asyncio tears down this
    # test's event loop.
    await asyncio.sleep(0.3)


async def test_stop_with_no_inflight_runs_is_a_no_op(
    db: Database, tmp_path: Path
) -> None:
    service, _repository = _service(db, tmp_path, runner=_FakeAgentRunner())

    await service.stop()  # nothing tracked — must not raise


# --- build_opencode_run_service(): the None-when-off factory --------------------------


def test_build_opencode_run_service_returns_none_when_runner_factory_returns_none(
    tmp_path: Path, db: Database
) -> None:
    settings = Settings(
        telemetry=TelemetrySettings(enabled=True),
        opencode=_opencode_settings(workspace_dir=tmp_path / "workspace"),
    )

    service = build_opencode_run_service(
        settings,
        db=db,
        runner_factory=lambda _settings: None,
    )

    assert service is None


def test_build_opencode_run_service_returns_none_when_telemetry_factory_returns_none(
    tmp_path: Path, db: Database
) -> None:
    settings = Settings(
        telemetry=TelemetrySettings(enabled=True),
        opencode=_opencode_settings(workspace_dir=tmp_path / "workspace"),
    )

    service = build_opencode_run_service(
        settings,
        db=db,
        runner_factory=lambda _settings: _FakeAgentRunner(),
        telemetry_factory=lambda _settings: None,
    )

    assert service is None


def test_build_opencode_run_service_returns_a_real_service_on_a_clean_pass(
    tmp_path: Path, db: Database
) -> None:
    settings = Settings(
        telemetry=TelemetrySettings(enabled=True),
        opencode=_opencode_settings(workspace_dir=tmp_path / "workspace"),
    )
    fake_runner = _FakeAgentRunner()
    fake_writer = _telemetry_writer(tmp_path)

    service = build_opencode_run_service(
        settings,
        db=db,
        runner_factory=lambda _settings: fake_runner,
        telemetry_factory=lambda _settings: fake_writer,
    )

    assert isinstance(service, OpencodeRunService)
    assert service.is_healthy() is True


# --- issue #78: the `OpencodeStateProbe` surface ------------------------------------


def test_counts_by_status_delegates_to_the_repository(
    db: Database, tmp_path: Path
) -> None:
    service, repository = _service(db, tmp_path, runner=_FakeAgentRunner())

    assert service.counts_by_status() == repository.counts_by_status()


async def test_running_runs_reports_started_at_with_no_last_event_at_before_any_event(
    db: Database, tmp_path: Path
) -> None:
    runner = _HangingAgentRunner()
    service, _repository = _service(db, tmp_path, runner=runner)

    run = await service.submit(RunRequest(prompt="say hello"))
    await asyncio.wait_for(runner.started.wait(), timeout=2.0)

    snapshots = service.running_runs()

    assert len(snapshots) == 1
    assert snapshots[0].id == run.id
    assert snapshots[0].started_at is not None
    assert snapshots[0].last_event_at is None
    await service.stop()


async def test_running_runs_reports_last_event_at_once_an_event_has_arrived(
    db: Database, tmp_path: Path
) -> None:
    runner = _RunnerThatEmitsThenHangs()
    service, _repository = _service(db, tmp_path, runner=runner)

    run = await service.submit(RunRequest(prompt="say hello"))
    await asyncio.wait_for(runner.started.wait(), timeout=2.0)

    snapshots = service.running_runs()

    assert snapshots[0].id == run.id
    assert snapshots[0].last_event_at is not None
    await service.stop()


async def test_last_event_at_is_forgotten_once_a_run_reaches_a_terminal_status(
    db: Database, tmp_path: Path
) -> None:
    """`_forget_task`'s done-callback pops `_last_event_at` the same way it already
    pops `_tasks` — this run's own in-memory tracking must not grow unbounded.
    """
    event = RunEvent(
        type=RunEventType.OTHER, raw_type="tool_call", payload="{}", sequence=0
    )
    runner = _FakeAgentRunner(events=[event])
    service, repository = _service(db, tmp_path, runner=runner)

    run = await service.submit(RunRequest(prompt="say hello"))
    await _wait_until(lambda: repository.get(run.id).status == "succeeded")  # type: ignore[union-attr]
    # Let the done-callback actually run — it fires on the next loop iteration
    # after the task finishes, not synchronously at that moment (the same
    # reasoning `test_cancel_returns_false_for_an_already_terminal_run` documents).
    await asyncio.sleep(0)

    assert run.id not in service._last_event_at
    assert service.running_runs() == []


async def test_recent_outcome_counts_delegates_to_the_repository(
    db: Database, tmp_path: Path
) -> None:
    service, repository = _service(db, tmp_path, runner=_FakeAgentRunner())
    since = "2020-01-01T00:00:00.000Z"

    counts = service.recent_outcome_counts(since=since)
    failed, timeout, permission_denied = repository.recent_outcome_counts(since)

    assert (counts.failed, counts.timeout, counts.permission_denied) == (
        failed,
        timeout,
        permission_denied,
    )
