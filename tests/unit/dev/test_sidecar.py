from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from ansina.dev.sidecar import ProcessHandle, SupervisedProcess, _spawn_subprocess


class FakeHandle:
    """A `ProcessHandle` double — `exit_code=None` means "never exits on its own";
    `terminate()`/`kill()` flip it to `terminated_exit_code` so `poll()`/`wait()`
    reflect a real process's behavior after a signal, without a real child.
    """

    def __init__(self, *, pid: int = 4242, exit_code: int | None = None) -> None:
        self.pid = pid
        self._exit_code = exit_code
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls: list[float | None] = []
        self.raise_on_terminate: Exception | None = None
        self.raise_on_wait: Exception | None = None
        self.raise_on_kill: Exception | None = None
        self.terminated_exit_code: int = -15

    def poll(self) -> int | None:
        return self._exit_code

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.raise_on_terminate is not None:
            raise self.raise_on_terminate

    def kill(self) -> None:
        self.kill_calls += 1
        if self.raise_on_kill is not None:
            raise self.raise_on_kill
        self._exit_code = self.terminated_exit_code

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self.raise_on_wait is not None:
            raise self.raise_on_wait
        if self._exit_code is None:
            self._exit_code = self.terminated_exit_code
        assert self._exit_code is not None
        return self._exit_code


def _make_process(
    *,
    spawner: Callable[..., ProcessHandle],
    sleeper: Callable[[float], None] | None = None,
) -> SupervisedProcess:
    return SupervisedProcess(
        argv=("vector", "--config", "deploy/vector.toml"),
        env={"PATH": "/usr/bin"},
        liveness_delay_seconds=0.0,
        shutdown_timeout_seconds=1.0,
        spawner=spawner,
        sleeper=sleeper if sleeper is not None else (lambda _seconds: None),
    )


def test_start_succeeds_when_the_child_is_still_alive_after_the_liveness_delay() -> (
    None
):
    handle = FakeHandle(exit_code=None)
    sleeps: list[float] = []

    def _spawn(
        argv: Sequence[str], *, env: Mapping[str, str], cwd: Path | None
    ) -> ProcessHandle:
        return handle

    process = _make_process(spawner=_spawn, sleeper=sleeps.append)

    assert process.start() is True
    assert process.is_alive() is True
    assert process.pid == handle.pid
    assert sleeps == [0.0]


def test_start_retries_once_then_gives_up_when_the_child_dies_immediately_twice() -> (
    None
):
    handles = [FakeHandle(pid=1, exit_code=1), FakeHandle(pid=2, exit_code=1)]
    spawned: list[ProcessHandle] = []

    def _spawn(
        argv: Sequence[str], *, env: Mapping[str, str], cwd: Path | None
    ) -> ProcessHandle:
        handle = handles[len(spawned)]
        spawned.append(handle)
        return handle

    process = _make_process(spawner=_spawn)

    assert process.start() is False
    assert process.is_alive() is False
    assert process.pid is None
    assert len(spawned) == 2


def test_start_succeeds_on_the_retry_after_the_first_spawn_dies_immediately() -> None:
    dead = FakeHandle(pid=1, exit_code=1)
    alive = FakeHandle(pid=2, exit_code=None)
    handles = [dead, alive]
    spawned: list[ProcessHandle] = []

    def _spawn(
        argv: Sequence[str], *, env: Mapping[str, str], cwd: Path | None
    ) -> ProcessHandle:
        handle = handles[len(spawned)]
        spawned.append(handle)
        return handle

    process = _make_process(spawner=_spawn)

    assert process.start() is True
    assert process.pid == alive.pid
    assert len(spawned) == 2


def test_start_retries_once_then_gives_up_on_a_repeated_oserror() -> None:
    attempts: list[int] = []

    def _spawn(
        argv: Sequence[str], *, env: Mapping[str, str], cwd: Path | None
    ) -> ProcessHandle:
        attempts.append(1)
        raise OSError("no such file or directory: vector")

    process = _make_process(spawner=_spawn)

    assert process.start() is False
    assert process.is_alive() is False
    assert len(attempts) == 2


def test_start_succeeds_on_the_retry_after_the_first_spawn_raises_oserror() -> None:
    alive = FakeHandle(pid=7, exit_code=None)
    attempts: list[int] = []

    def _spawn(
        argv: Sequence[str], *, env: Mapping[str, str], cwd: Path | None
    ) -> ProcessHandle:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("transient")
        return alive

    process = _make_process(spawner=_spawn)

    assert process.start() is True
    assert process.pid == alive.pid


def test_argv_and_env_are_passed_through_to_the_spawner() -> None:
    captured: dict[str, Any] = {}

    def _spawn(
        argv: Sequence[str], *, env: Mapping[str, str], cwd: Path | None
    ) -> ProcessHandle:
        captured["argv"] = tuple(argv)
        captured["env"] = dict(env)
        captured["cwd"] = cwd
        return FakeHandle()

    process = SupervisedProcess(
        argv=("vector", "--config", "deploy/vector.toml"),
        env={"PATH": "/usr/bin", "FOO": "bar"},
        cwd=Path("/some/dir"),
        liveness_delay_seconds=0.0,
        shutdown_timeout_seconds=1.0,
        spawner=_spawn,
        sleeper=lambda _seconds: None,
    )
    assert process.argv == ("vector", "--config", "deploy/vector.toml")
    process.start()

    assert captured["argv"] == ("vector", "--config", "deploy/vector.toml")
    assert captured["env"] == {"PATH": "/usr/bin", "FOO": "bar"}
    assert captured["cwd"] == Path("/some/dir")


def test_stop_with_no_child_ever_started_is_a_no_op() -> None:
    process = _make_process(spawner=lambda *a, **k: FakeHandle())
    process.stop()  # never started
    process.stop()  # idempotent


def test_stop_terminates_cleanly_when_the_child_exits_in_time() -> None:
    handle = FakeHandle(exit_code=None)
    process = _make_process(spawner=lambda *a, **k: handle)
    process.start()

    process.stop()

    assert handle.terminate_calls == 1
    assert handle.kill_calls == 0
    assert process.is_alive() is False


def test_stop_twice_in_a_row_is_idempotent() -> None:
    handle = FakeHandle(exit_code=None)
    process = _make_process(spawner=lambda *a, **k: handle)
    process.start()

    process.stop()
    process.stop()  # second call: no handle left, must not re-terminate

    assert handle.terminate_calls == 1


def test_stop_kills_after_a_timeout_waiting_for_terminate() -> None:
    handle = FakeHandle(exit_code=None)
    process = _make_process(spawner=lambda *a, **k: handle)
    process.start()

    first_wait = subprocess.TimeoutExpired(cmd="vector", timeout=1.0)

    def _wait(timeout: float | None = None) -> int:
        handle.wait_calls.append(timeout)
        if len(handle.wait_calls) == 1:
            raise first_wait
        handle._exit_code = handle.terminated_exit_code
        return handle.terminated_exit_code

    handle.wait = _wait  # type: ignore[method-assign]

    process.stop()

    assert handle.terminate_calls == 1
    assert handle.kill_calls == 1


def test_stop_swallows_a_process_lookup_error_from_an_already_dead_child() -> None:
    handle = FakeHandle(exit_code=None)
    process = _make_process(spawner=lambda *a, **k: handle)
    process.start()
    handle.raise_on_terminate = ProcessLookupError("already reaped")

    process.stop()  # must not raise


def test_stop_force_kill_swallows_a_process_lookup_error_too() -> None:
    handle = FakeHandle(exit_code=None)
    process = _make_process(spawner=lambda *a, **k: handle)
    process.start()
    handle.raise_on_wait = subprocess.TimeoutExpired(cmd="vector", timeout=1.0)
    handle.raise_on_kill = ProcessLookupError("already reaped by the time we got here")

    process.stop()  # must not raise


def test_is_alive_is_false_before_start_and_after_the_child_exits() -> None:
    handle = FakeHandle(exit_code=None)
    process = _make_process(spawner=lambda *a, **k: handle)
    assert process.is_alive() is False

    process.start()
    assert process.is_alive() is True

    handle._exit_code = 0
    assert process.is_alive() is False


def test_spawn_subprocess_sets_start_new_session_and_inherits_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`stdout`/`stderr` are deliberately left unset (`None`, inherited) — see
    `_spawn_subprocess`'s own docstring: Vector's own diagnostics must land
    wherever the daemon's own stdout/stderr already goes, not a silent
    `DEVNULL`.
    """
    captured: dict[str, Any] = {}

    class _FakePopen:
        def __init__(self, argv: Sequence[str], **kwargs: Any) -> None:
            captured["argv"] = list(argv)
            captured.update(kwargs)
            self.pid = 999

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    handle = _spawn_subprocess(
        ["vector", "--config", "x.toml"], env={"PATH": "/usr/bin"}, cwd=None
    )

    assert isinstance(handle, _FakePopen)
    assert captured["argv"] == ["vector", "--config", "x.toml"]
    assert captured["env"] == {"PATH": "/usr/bin"}
    assert captured["start_new_session"] is True
    assert "stdout" not in captured
    assert "stderr" not in captured
