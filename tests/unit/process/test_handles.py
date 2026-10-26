from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from ansina.process.handles import (
    PipedProcessHandle,
    ProcessHandle,
    spawn_piped_subprocess,
    spawn_subprocess,
)


def test_spawn_subprocess_sets_start_new_session_and_inherits_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors `tests/unit/dev/test_sidecar.py`'s own test for the pre-#74
    `_spawn_subprocess` (now an alias for this function) — `stdout`/`stderr`
    are deliberately left unset (`None`, inherited), not silently redirected
    to `DEVNULL`.
    """
    captured: dict[str, Any] = {}

    class _FakePopen:
        def __init__(self, argv: Any, **kwargs: Any) -> None:
            captured["argv"] = list(argv)
            captured.update(kwargs)
            self.pid = 999

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    handle = spawn_subprocess(["true", "--flag"], env={"PATH": "/usr/bin"}, cwd=None)

    assert isinstance(handle, _FakePopen)
    assert captured["argv"] == ["true", "--flag"]
    assert captured["env"] == {"PATH": "/usr/bin"}
    assert captured["start_new_session"] is True
    assert "stdout" not in captured
    assert "stderr" not in captured


def test_spawn_subprocess_against_a_real_child_satisfies_processhandle() -> None:
    """A real `subprocess.Popen` call, not a fake — proves `spawn_subprocess`'s
    real return value structurally satisfies `ProcessHandle` (`pid`, `poll`,
    `wait`) with no wrapper.
    """
    handle: ProcessHandle = spawn_subprocess(
        [sys.executable, "-c", "pass"], env={"PATH": "/usr/bin"}, cwd=None
    )
    exit_code = handle.wait(timeout=5.0)
    assert exit_code == 0
    assert handle.poll() == 0
    assert isinstance(handle.pid, int)


def test_spawn_piped_subprocess_sets_start_new_session_and_pipe_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _FakePopen:
        def __init__(self, argv: Any, **kwargs: Any) -> None:
            captured["argv"] = list(argv)
            captured.update(kwargs)
            self.pid = 1000

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    handle = spawn_piped_subprocess(["true"], env={"PATH": "/usr/bin"}, cwd=None)

    assert isinstance(handle, _FakePopen)
    assert captured["argv"] == ["true"]
    assert captured["env"] == {"PATH": "/usr/bin"}
    assert captured["start_new_session"] is True
    assert captured["stdout"] is subprocess.PIPE
    assert captured["stderr"] is subprocess.PIPE
    assert captured["text"] is True
    assert captured["bufsize"] == 1


def test_spawn_piped_subprocess_against_a_real_child_streams_text_lines() -> None:
    """A real child, not a fake — proves the pipes really are text-mode and
    really are readable line by line, the whole point of `PipedProcessHandle`
    over the bare `ProcessHandle` `spawn_subprocess` returns.
    """
    handle: PipedProcessHandle = spawn_piped_subprocess(
        [sys.executable, "-c", "print('one'); print('two')"],
        env={"PATH": "/usr/bin"},
        cwd=None,
    )
    assert handle.stdout is not None
    lines = [line.rstrip("\n") for line in handle.stdout]
    assert lines == ["one", "two"]
    assert handle.wait(timeout=5.0) == 0
    assert handle.stderr is not None
