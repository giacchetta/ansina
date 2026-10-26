from __future__ import annotations

import asyncio
import itertools
import json
import logging
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from ansina.config.settings import OpencodeSettings, Settings, TelemetrySettings
from ansina.errors import (
    OpencodeBusyError,
    OpencodeRunFailedError,
    OpencodeUnavailableError,
)
from ansina.logging.redaction import clear_secrets, register_secret
from ansina.opencode.events import RunEvent, RunRequest, RunStatus
from ansina.opencode.runner import AgentRunner, OpencodeCliRunner
from ansina.process.handles import PipedProcessHandle


def _settings(tmp_path: Path, **opencode_kwargs: Any) -> Settings:
    """Same `Settings(telemetry=..., opencode=...)` shape every other
    `ansina.opencode` test module uses (`tests/unit/opencode/test_workspace.
    py`'s own `_settings` docstring explains why both are passed as complete
    model instances) — `workspace_dir` always anchored to `tmp_path` so this
    never touches a real directory or a real `ansina.toml`.
    """
    opencode_kwargs.setdefault("workspace_dir", tmp_path / "workspace")
    return Settings(
        telemetry=TelemetrySettings(enabled=True),
        opencode=OpencodeSettings(enabled=True, **opencode_kwargs),
    )


class _FakeStdout:
    """A controllable, blocking line iterator standing in for a real pipe.

    `__next__` blocks on an internal queue until a line is `push()`ed or the
    pipe is `close()`d (simulating EOF — either a natural exit or the
    process dying from `terminate()`/`kill()`) — exactly the two ways a real
    `Popen.stdout` ever stops yielding lines.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._closed = False

    def push(self, line: str) -> None:
        self._queue.put(line)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._queue.put(None)

    def __iter__(self) -> _FakeStdout:
        return self

    def __next__(self) -> str:
        item = self._queue.get()
        if item is None:
            raise StopIteration
        return item


class _FakePipedProcessHandle:
    """A fake `PipedProcessHandle`. `ignore_terminate=True` simulates a child
    that does not respond to `terminate()` (forcing the timeout path's own
    `kill()` fallback); otherwise `terminate()` finishes the fake the same
    way `kill()` always does — closing `stdout` (EOF) and fixing the exit
    code `wait()`/`poll()` report.
    """

    def __init__(self, *, ignore_terminate: bool = False) -> None:
        self.pid = 4242
        self.stdout: _FakeStdout = _FakeStdout()
        self.stderr: _FakeStdout | None = None
        self.terminate_called = False
        self.kill_called = False
        self.call_order: list[str] = []
        self._ignore_terminate = ignore_terminate
        self._exit_code: int | None = None
        self._finished = threading.Event()

    def complete(self, exit_code: int = 0) -> None:
        """Test-only: the child exits naturally — never via `terminate()`/
        `kill()`.
        """
        self._finish(exit_code)

    def poll(self) -> int | None:
        return self._exit_code

    def terminate(self) -> None:
        self.terminate_called = True
        self.call_order.append("terminate")
        if not self._ignore_terminate:
            self._finish(0)

    def kill(self) -> None:
        self.kill_called = True
        self.call_order.append("kill")
        self._finish(-9)

    def wait(self, timeout: float | None = None) -> int:
        exit_code = self._exit_code
        if exit_code is not None:
            return exit_code
        if not self._finished.wait(timeout=timeout):
            raise subprocess.TimeoutExpired(cmd=["opencode"], timeout=timeout or 0.0)
        # Re-read rather than trust mypy's own (here, wrong) narrowing of the
        # first check above: `_finish()` sets this from a *different* thread
        # (the runner's reader thread or the real caller), which mypy's
        # single-function control-flow analysis has no way to see.
        exit_code = self._exit_code
        assert exit_code is not None
        return exit_code

    def _finish(self, exit_code: int) -> None:
        if self._exit_code is not None:
            return
        self._exit_code = exit_code
        self.stdout.close()
        self._finished.set()


def _make_spawner(
    handle: _FakePipedProcessHandle, captured: dict[str, Any] | None = None
) -> Callable[..., PipedProcessHandle]:
    """`handle`'s own `stdout`/`stderr` are the narrow, duck-typed
    `_FakeStdout` this module needs (`.push()`/`.close()`), not a real
    `IO[str]` — `cast` below is the honest boundary crossing: at runtime,
    `OpencodeCliRunner`'s own `_read_stdout` only ever iterates `stdout` line
    by line, which `_FakeStdout` genuinely satisfies, even though it doesn't
    implement the rest of `IO[str]`'s stdlib surface.
    """

    def _spawner(
        argv: Sequence[str], *, env: Mapping[str, str], cwd: Path | None
    ) -> PipedProcessHandle:
        if captured is not None:
            captured["argv"] = list(argv)
            captured["env"] = dict(env)
            captured["cwd"] = cwd
        return cast(PipedProcessHandle, handle)

    return _spawner


# --- AgentRunner / OpencodeCliRunner is a real AgentRunner ------------------


def test_opencode_cli_runner_satisfies_agent_runner_protocol(tmp_path: Path) -> None:
    runner: AgentRunner = OpencodeCliRunner(
        _settings(tmp_path), opencode_path="/usr/bin/opencode"
    )
    assert isinstance(runner, AgentRunner)


def test_opencode_path_property_exposes_the_resolved_binary(tmp_path: Path) -> None:
    runner = OpencodeCliRunner(_settings(tmp_path), opencode_path="/usr/bin/opencode")
    assert runner.opencode_path == "/usr/bin/opencode"


# --- admission control: raises before any subprocess exists -----------------


async def test_run_raises_unavailable_when_no_opencode_path(tmp_path: Path) -> None:
    def _never_spawn(*_args: Any, **_kwargs: Any) -> PipedProcessHandle:
        raise AssertionError("must not spawn when unavailable")

    runner = OpencodeCliRunner(
        _settings(tmp_path), opencode_path=None, spawner=_never_spawn
    )

    with pytest.raises(OpencodeUnavailableError):
        await runner.run(RunRequest(prompt="hi"), lambda _e: None)


async def test_run_raises_run_failed_when_prompt_too_long(tmp_path: Path) -> None:
    spawn_count = 0

    def _spawner(*_args: Any, **_kwargs: Any) -> PipedProcessHandle:
        nonlocal spawn_count
        spawn_count += 1
        return cast(PipedProcessHandle, _FakePipedProcessHandle())

    settings = _settings(tmp_path, max_prompt_chars=5)
    runner = OpencodeCliRunner(settings, opencode_path="x", spawner=_spawner)

    with pytest.raises(OpencodeRunFailedError):
        await runner.run(RunRequest(prompt="too long"), lambda _e: None)

    assert spawn_count == 0


async def test_run_raises_busy_when_queue_max_depth_exceeded(tmp_path: Path) -> None:
    handle = _FakePipedProcessHandle()
    spawn_count = 0

    def _spawner(*_args: Any, **_kwargs: Any) -> PipedProcessHandle:
        nonlocal spawn_count
        spawn_count += 1
        return cast(PipedProcessHandle, handle)

    settings = _settings(tmp_path, max_concurrent_runs=1, queue_max_depth=1)
    runner = OpencodeCliRunner(settings, opencode_path="x", spawner=_spawner)
    task = asyncio.create_task(runner.run(RunRequest(prompt="p0"), lambda _e: None))
    await asyncio.sleep(0.05)  # let the first call acquire admission and spawn

    with pytest.raises(OpencodeBusyError):
        await runner.run(RunRequest(prompt="p1"), lambda _e: None)

    assert spawn_count == 1  # the busy call never reached the spawner
    handle.complete(0)
    outcome = await task
    assert outcome.status is RunStatus.COMPLETED


# --- spawn failure: a RunOutcome, never a raise ------------------------------


async def test_spawn_oserror_becomes_a_failed_outcome(tmp_path: Path) -> None:
    def _spawner(*_args: Any, **_kwargs: Any) -> PipedProcessHandle:
        raise OSError("no such file or directory: opencode")

    runner = OpencodeCliRunner(_settings(tmp_path), opencode_path="x", spawner=_spawner)

    outcome = await runner.run(RunRequest(prompt="hi"), lambda _e: None)

    assert outcome.status is RunStatus.FAILED
    assert outcome.exit_code is None
    assert outcome.error is not None
    assert "no such file" in outcome.error


# --- argv construction --------------------------------------------------------


async def test_argv_includes_prompt_format_and_workspace_dir(tmp_path: Path) -> None:
    handle = _FakePipedProcessHandle()
    handle.complete(0)
    captured: dict[str, Any] = {}
    settings = _settings(tmp_path)
    runner = OpencodeCliRunner(
        settings,
        opencode_path="/usr/bin/opencode",
        spawner=_make_spawner(handle, captured),
    )

    await runner.run(RunRequest(prompt="do the thing"), lambda _e: None)

    argv = captured["argv"]
    assert argv[0] == "/usr/bin/opencode"
    assert argv[1] == "run"
    assert argv[2] == "do the thing"
    assert argv[argv.index("--format") + 1] == "json"
    assert argv[argv.index("--dir") + 1] == str(settings.opencode.workspace_dir)
    assert captured["cwd"] == settings.opencode.workspace_dir


async def test_argv_omits_optional_flags_when_nothing_is_set(tmp_path: Path) -> None:
    handle = _FakePipedProcessHandle()
    handle.complete(0)
    captured: dict[str, Any] = {}
    settings = _settings(tmp_path, model="", agent="", auto_approve=False)
    runner = OpencodeCliRunner(
        settings, opencode_path="x", spawner=_make_spawner(handle, captured)
    )

    await runner.run(RunRequest(prompt="p"), lambda _e: None)

    argv = captured["argv"]
    for flag in ("--session", "--continue", "--fork", "--model", "--agent", "--auto"):
        assert flag not in argv


async def test_argv_includes_session_fork_model_agent_auto_from_request(
    tmp_path: Path,
) -> None:
    handle = _FakePipedProcessHandle()
    handle.complete(0)
    captured: dict[str, Any] = {}
    settings = _settings(tmp_path, model="", agent="", auto_approve=False)
    runner = OpencodeCliRunner(
        settings, opencode_path="x", spawner=_make_spawner(handle, captured)
    )
    request = RunRequest(
        prompt="p",
        session_id="sess-1",
        fork=True,
        model="anthropic/claude",
        agent="build",
        auto_approve=True,
    )

    await runner.run(request, lambda _e: None)

    argv = captured["argv"]
    assert argv[argv.index("--session") + 1] == "sess-1"
    assert "--fork" in argv
    assert "--continue" not in argv
    assert argv[argv.index("--model") + 1] == "anthropic/claude"
    assert argv[argv.index("--agent") + 1] == "build"
    assert "--auto" in argv


async def test_argv_includes_continue_flag(tmp_path: Path) -> None:
    handle = _FakePipedProcessHandle()
    handle.complete(0)
    captured: dict[str, Any] = {}
    runner = OpencodeCliRunner(
        _settings(tmp_path), opencode_path="x", spawner=_make_spawner(handle, captured)
    )

    await runner.run(RunRequest(prompt="p", continue_last=True), lambda _e: None)

    argv = captured["argv"]
    assert "--continue" in argv
    assert "--session" not in argv


async def test_argv_falls_back_to_configured_model_agent_auto(tmp_path: Path) -> None:
    handle = _FakePipedProcessHandle()
    handle.complete(0)
    captured: dict[str, Any] = {}
    settings = _settings(
        tmp_path, model="configured/model", agent="configured-agent", auto_approve=True
    )
    runner = OpencodeCliRunner(
        settings, opencode_path="x", spawner=_make_spawner(handle, captured)
    )

    await runner.run(RunRequest(prompt="p"), lambda _e: None)

    argv = captured["argv"]
    assert argv[argv.index("--model") + 1] == "configured/model"
    assert argv[argv.index("--agent") + 1] == "configured-agent"
    assert "--auto" in argv


async def test_argv_request_model_overrides_configured_model(tmp_path: Path) -> None:
    handle = _FakePipedProcessHandle()
    handle.complete(0)
    captured: dict[str, Any] = {}
    settings = _settings(tmp_path, model="configured/model")
    runner = OpencodeCliRunner(
        settings, opencode_path="x", spawner=_make_spawner(handle, captured)
    )

    await runner.run(RunRequest(prompt="p", model="override/model"), lambda _e: None)

    argv = captured["argv"]
    assert argv[argv.index("--model") + 1] == "override/model"


# --- streaming: events delivered to the callback as lines arrive -----------


async def test_events_are_delivered_one_at_a_time_as_lines_arrive(
    tmp_path: Path,
) -> None:
    """Each subsequent line is only pushed onto the fake pipe *after* the
    previous one was actually delivered to the callback — if the
    implementation only read everything at the very end (batched), this
    would hang until `run_timeout_seconds` and the run would end in
    `TIMEOUT`, not `COMPLETED`.
    """
    handle = _FakePipedProcessHandle()
    total = 4
    delivered: list[int] = []

    def on_event(event: RunEvent) -> None:
        payload = json.loads(event.payload)
        delivered.append(payload["i"])
        next_index = len(delivered)
        if next_index < total:
            handle.stdout.push(json.dumps({"type": "a", "i": next_index}))
        else:
            handle.complete(0)

    handle.stdout.push(json.dumps({"type": "a", "i": 0}))
    settings = _settings(tmp_path, run_timeout_seconds=5.0)
    runner = OpencodeCliRunner(
        settings, opencode_path="x", spawner=_make_spawner(handle)
    )

    outcome = await runner.run(RunRequest(prompt="hi"), on_event)

    assert delivered == [0, 1, 2, 3]
    assert outcome.status is RunStatus.COMPLETED
    assert outcome.events_emitted == 4
    assert outcome.events_skipped == 0


async def test_malformed_line_is_skipped_never_raised_never_delivered(
    tmp_path: Path,
) -> None:
    handle = _FakePipedProcessHandle()
    handle.stdout.push("not json at all")
    handle.stdout.push(json.dumps({"type": "a"}))
    handle.complete(0)
    delivered: list[RunEvent] = []
    settings = _settings(tmp_path, run_timeout_seconds=5.0)
    runner = OpencodeCliRunner(
        settings, opencode_path="x", spawner=_make_spawner(handle)
    )

    outcome = await runner.run(RunRequest(prompt="hi"), delivered.append)

    assert len(delivered) == 1
    assert outcome.events_emitted == 1
    assert outcome.events_skipped == 1
    assert outcome.status is RunStatus.COMPLETED


async def test_exit_code_zero_with_clean_eof_is_completed(tmp_path: Path) -> None:
    handle = _FakePipedProcessHandle()
    handle.complete(0)
    settings = _settings(tmp_path, run_timeout_seconds=5.0)
    runner = OpencodeCliRunner(
        settings, opencode_path="x", spawner=_make_spawner(handle)
    )

    outcome = await runner.run(RunRequest(prompt="hi"), lambda _e: None)

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.exit_code == 0
    assert outcome.error is None


async def test_nonzero_exit_code_is_a_failed_outcome(tmp_path: Path) -> None:
    handle = _FakePipedProcessHandle()
    handle.complete(1)
    settings = _settings(tmp_path, run_timeout_seconds=5.0)
    runner = OpencodeCliRunner(
        settings, opencode_path="x", spawner=_make_spawner(handle)
    )

    outcome = await runner.run(RunRequest(prompt="hi"), lambda _e: None)

    assert outcome.status is RunStatus.FAILED
    assert outcome.exit_code == 1
    assert outcome.error is not None


# --- max_run_output_bytes: truncates, still a RunOutcome --------------------


async def test_max_run_output_bytes_exceeded_truncates_and_fails(
    tmp_path: Path,
) -> None:
    handle = _FakePipedProcessHandle()
    handle.stdout.push(json.dumps({"type": "a", "pad": "x" * 200}))
    handle.complete(0)
    settings = _settings(
        tmp_path, run_timeout_seconds=5.0, max_event_bytes=16, max_run_output_bytes=32
    )
    runner = OpencodeCliRunner(
        settings, opencode_path="x", spawner=_make_spawner(handle)
    )

    outcome = await runner.run(RunRequest(prompt="hi"), lambda _e: None)

    assert outcome.truncated is True
    assert outcome.status is RunStatus.FAILED
    assert outcome.error is not None


# --- timeout: terminate -> kill, reader thread does not outlive the kill ---


async def test_timeout_terminates_then_kills_and_returns_promptly(
    tmp_path: Path,
) -> None:
    """A hung child (ignores `terminate()`) must still be killed, and the
    whole `run()` call must return within a small, bounded multiple of
    `run_timeout_seconds` + `shutdown_timeout_seconds` — proving the reader
    thread was joined promptly rather than left blocked past the kill.
    """
    handle = _FakePipedProcessHandle(ignore_terminate=True)
    settings = _settings(
        tmp_path, run_timeout_seconds=0.05, shutdown_timeout_seconds=0.05
    )
    runner = OpencodeCliRunner(
        settings, opencode_path="x", spawner=_make_spawner(handle)
    )

    start = time.monotonic()
    outcome = await runner.run(RunRequest(prompt="hi"), lambda _e: None)
    elapsed = time.monotonic() - start

    assert outcome.status is RunStatus.TIMEOUT
    assert handle.call_order == ["terminate", "kill"]
    assert elapsed < 2.0  # generous bound; the real wait is ~0.1s


async def test_timeout_check_at_top_of_loop_after_processing_an_event(
    tmp_path: Path,
) -> None:
    """A different branch than a genuinely-blocking `asyncio.wait_for` timing
    out (`except TimeoutError`, exercised above): here the deadline has
    already passed by the time the loop re-checks it at the very top of a
    *later* iteration, right after successfully processing a line — proven
    with an injected `clock` rather than real sleeping, since the real
    wall-clock gap between two loop iterations processing one already-queued
    line is far too small to reach a timeout on its own.
    """
    handle = _FakePipedProcessHandle()
    handle.stdout.push(json.dumps({"type": "a"}))
    clock_values = itertools.chain([0.0, 0.0, 10.0], itertools.repeat(10.0))
    settings = _settings(
        tmp_path, run_timeout_seconds=1.0, shutdown_timeout_seconds=0.05
    )
    runner = OpencodeCliRunner(
        settings,
        opencode_path="x",
        spawner=_make_spawner(handle),
        clock=lambda: next(clock_values),
    )

    outcome = await runner.run(RunRequest(prompt="hi"), lambda _e: None)

    assert outcome.status is RunStatus.TIMEOUT
    assert outcome.events_emitted == 1


class _SlowToExitStdout:
    """Simulates a reader thread that outlives `shutdown_timeout_seconds`'s
    own join but does eventually exit shortly after on its own — proves the
    "reader thread did not exit in time" warning path without leaking a
    permanently-blocked daemon thread for the rest of the test run.
    """

    def __iter__(self) -> _SlowToExitStdout:
        return self

    def __next__(self) -> str:
        time.sleep(0.2)
        raise StopIteration

    def close(self) -> None:
        pass


async def test_logs_a_warning_when_the_reader_thread_outlives_the_join(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    handle = _FakePipedProcessHandle()
    handle.stdout = _SlowToExitStdout()  # type: ignore[assignment]
    settings = _settings(
        tmp_path, run_timeout_seconds=0.02, shutdown_timeout_seconds=0.02
    )
    runner = OpencodeCliRunner(
        settings, opencode_path="x", spawner=_make_spawner(handle)
    )

    with caplog.at_level(logging.WARNING):
        outcome = await runner.run(RunRequest(prompt="hi"), lambda _e: None)

    assert outcome.status is RunStatus.TIMEOUT
    assert any(
        "reader thread did not exit in time" in record.message
        for record in caplog.records
    )
    # The reader thread outlives the join above by design (that's the whole
    # point of this test) but does still exit shortly after, on its own —
    # wait for it here, on this test's still-open event loop, so its final
    # `call_soon_threadsafe` lands before the loop closes rather than racing
    # the test runner's own teardown (which would otherwise surface as an
    # unrelated `RuntimeError: Event loop is closed` in a *later* test).
    await asyncio.sleep(0.3)


async def test_timeout_does_not_kill_a_child_that_terminates_promptly(
    tmp_path: Path,
) -> None:
    handle = _FakePipedProcessHandle()  # terminate() finishes it immediately
    settings = _settings(
        tmp_path, run_timeout_seconds=0.05, shutdown_timeout_seconds=1.0
    )
    runner = OpencodeCliRunner(
        settings, opencode_path="x", spawner=_make_spawner(handle)
    )

    outcome = await runner.run(RunRequest(prompt="hi"), lambda _e: None)

    assert outcome.status is RunStatus.TIMEOUT
    assert handle.call_order == ["terminate"]
    assert handle.kill_called is False


# --- cancellation propagates untouched --------------------------------------


async def test_cancellation_propagates_and_terminates_the_child(
    tmp_path: Path,
) -> None:
    handle = _FakePipedProcessHandle()  # never completes on its own
    settings = _settings(tmp_path, run_timeout_seconds=5.0)
    runner = OpencodeCliRunner(
        settings, opencode_path="x", spawner=_make_spawner(handle)
    )
    task = asyncio.create_task(runner.run(RunRequest(prompt="hi"), lambda _e: None))
    await asyncio.sleep(0.05)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert handle.terminate_called is True


# --- concurrency: max_concurrent_runs bounds active spawns -----------------


async def test_max_concurrent_runs_bounds_active_spawns(tmp_path: Path) -> None:
    handles = [_FakePipedProcessHandle() for _ in range(3)]
    spawn_order: list[int] = []
    index = 0

    def _spawner(*_args: Any, **_kwargs: Any) -> PipedProcessHandle:
        nonlocal index
        handle = handles[index]
        spawn_order.append(index)
        index += 1
        return cast(PipedProcessHandle, handle)

    settings = _settings(
        tmp_path, max_concurrent_runs=2, queue_max_depth=5, run_timeout_seconds=5.0
    )
    runner = OpencodeCliRunner(settings, opencode_path="x", spawner=_spawner)

    tasks = [
        asyncio.create_task(runner.run(RunRequest(prompt=f"p{i}"), lambda _e: None))
        for i in range(3)
    ]
    await asyncio.sleep(0.1)

    assert spawn_order == [0, 1]  # the third call is still waiting on the semaphore

    handles[0].complete(0)
    await asyncio.sleep(0.1)

    assert spawn_order == [0, 1, 2]  # releasing the first let the third spawn

    handles[1].complete(0)
    handles[2].complete(0)
    outcomes = await asyncio.gather(*tasks)
    assert all(outcome.status is RunStatus.COMPLETED for outcome in outcomes)


# --- redaction: a registered secret never reaches a delivered event --------


async def test_a_registered_secret_never_reaches_a_delivered_event(
    tmp_path: Path,
) -> None:
    register_secret("sk-super-secret-value")
    try:
        handle = _FakePipedProcessHandle()
        handle.stdout.push(json.dumps({"type": "a", "token": "sk-super-secret-value"}))
        handle.complete(0)
        delivered: list[RunEvent] = []
        settings = _settings(tmp_path, run_timeout_seconds=5.0)
        runner = OpencodeCliRunner(
            settings, opencode_path="x", spawner=_make_spawner(handle)
        )

        await runner.run(RunRequest(prompt="hi"), delivered.append)

        assert len(delivered) == 1
        assert "sk-super-secret-value" not in delivered[0].payload
    finally:
        clear_secrets()
