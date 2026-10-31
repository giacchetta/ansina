"""Shared subprocess primitives for any Ansina subsystem that spawns and
supervises a child process. First shipped under `ansina.dev.sidecar` (issue #62,
a single lab-mode sidecar); lifted out here, unchanged, by issue #74 once a second
subsystem (`ansina.opencode`) needed the identical primitives for a differently-
shaped child. `ansina.dev.sidecar` re-exports these names unchanged, so every
existing caller and test is unaffected by the move.

`ProcessHandle`/`Spawner` are structural `Protocol`s (the same narrow-slice-of-the-
stdlib pattern `heart.eval.provenance.GitRunner` already establishes) so the unit
suite injects a fake with no real child process. `PipedProcessHandle`/
`PipedSpawner` widen `ProcessHandle`/`Spawner` by inheritance — the same
`TickController(TickLifecycle, Protocol)` / `ReadinessRegistry(ReadinessProbe,
Protocol)` precedent (`heart/tick/loop.py`, `heart/tick/sources/daemon_state.py`)
— for a caller that needs to read the child's own stdout/stderr as it streams,
rather than only supervise its lifecycle; `ansina.opencode`'s one-shot runner
(issue #75) is the first such caller.

This is also where the codebase's "the daemon itself never shells out" invariant
(`heart.eval.provenance`'s own module docstring) is deliberately narrowed, not
broken: `subprocess` is used here, but only ever reached via a module this
project explicitly names as an exception — `ansina.dev` (reached once, at boot,
before uvicorn binds a port) and `ansina.opencode` (reached at runtime, repeatedly
— see that package's own module docstring for the further-widened invariant issue
#75 introduces) — never from an arbitrary module.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import IO, Protocol


class ProcessHandle(Protocol):
    """The exact slice of `subprocess.Popen` a supervisor calls — narrow enough
    that a real `Popen` satisfies it with no wrapper, and the unit suite can
    inject a fake with no real child process.
    """

    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class Spawner(Protocol):
    """`(argv, env, cwd) -> ProcessHandle` — the one seam `spawn_subprocess` fills
    in production; the unit suite injects a fake that never spawns a real process.
    """

    def __call__(
        self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path | None
    ) -> ProcessHandle: ...


def spawn_subprocess(
    argv: Sequence[str], *, env: Mapping[str, str], cwd: Path | None
) -> ProcessHandle:
    """The one production `Spawner` for a supervised-but-not-piped child.
    `start_new_session=True` is load-bearing — the child sits outside the
    caller's own signal group, so a SIGTERM/SIGINT to the daemon does not also
    hit it uncontrolled; the daemon terminates it deliberately instead, via
    `SupervisedProcess.stop()`. `argv`/`env` are always built by the caller from
    typed `Settings` fields, never from unsanitized/shell-interpolated input.

    `stdout`/`stderr` are left `None` — the child inherits the caller's own file
    descriptors, so its own startup/connectivity diagnostics land wherever the
    daemon's own stdout/stderr already goes (a real operator's terminal, or
    whatever redirects it, e.g. `scripts/dev-mode-smoke.sh`'s `run.log`). Dev
    Mode's whole point is visibility ("I need to understand what it does"), not a
    silently detached child — see `spawn_piped_subprocess` below for the one
    caller that needs the opposite.
    """
    return subprocess.Popen(
        list(argv),
        env=dict(env),
        cwd=cwd,
        start_new_session=True,
    )


Sleeper = Callable[[float], None]


class PipedProcessHandle(ProcessHandle, Protocol):
    """`ProcessHandle` plus the child's own stdout/stderr pipes — the wider
    capability a streaming reader (issue #75's line-delimited-JSON event loop)
    needs, that a bare supervised child does not. `stdout`/`stderr` are read-only
    `@property`s, not plain attributes — the same reason `ClassifiedOutcome.
    expected` (`heart/eval/metrics.py`) is a `@property` on its own Protocol: a
    plain-attribute Protocol member demands a *settable* field, and a real
    `Popen`'s own `stdout`/`stderr` are themselves fixed once the child is
    spawned.
    """

    @property
    def stdout(self) -> IO[str] | None: ...

    @property
    def stderr(self) -> IO[str] | None: ...


class PipedSpawner(Protocol):
    """`(argv, env, cwd) -> PipedProcessHandle` — `Spawner`'s piped counterpart."""

    def __call__(
        self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path | None
    ) -> PipedProcessHandle: ...


def spawn_piped_subprocess(
    argv: Sequence[str], *, env: Mapping[str, str], cwd: Path | None
) -> PipedProcessHandle:
    """The one production `PipedSpawner`: `stdout`/`stderr` are pipes (`text=True`,
    line-buffered via `bufsize=1`) rather than inherited, so a caller can read
    each line as it arrives instead of only at exit — issue #75's streaming
    JSON-event reader is the first caller. `start_new_session=True` for the
    identical reason `spawn_subprocess` sets it.
    """
    return subprocess.Popen(
        list(argv),
        env=dict(env),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
