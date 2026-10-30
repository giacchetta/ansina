"""Generic subprocess supervision for a single background child. See issue #62.

Not a plugin/sidecar framework — "design the preflight/spawn/supervise shape so a
second occupant is a plausible future append, but do not build speculative
generality for it now" is the issue's own instruction. `ProcessHandle`/`Spawner`
(and their piped siblings) moved to `ansina.process.handles` as of issue #74, once
a second subsystem (`ansina.opencode`) needed the identical primitives — imported
back in here unchanged, so this module's own public surface (and every existing
caller/test) stays byte-for-byte the same. `SupervisedProcess` is the one concrete
lifecycle (`start`/`is_alive`/`stop`) `ansina.dev.vector`'s one real occupant
builds on.

This is also where the codebase's "the daemon itself never shells out"
invariant (`heart.eval.provenance`'s own module docstring) is deliberately narrowed,
not broken: `subprocess` is used here, but only ever reached via `ansina.dev`, and
only ever constructed once, at boot, by `__main__.py` before uvicorn binds a port —
never from `create_app()`, its lifespan, a route, or any periodic loop. (`ansina.
opencode` narrows the same invariant differently, to runtime/repeated use — see
`ansina.process.handles`'s own module docstring and `ansina.opencode`'s, issue #75.)
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from ansina.logging import get_logger
from ansina.process.handles import ProcessHandle, Sleeper, Spawner, spawn_subprocess

logger = get_logger(__name__)

# Explicit, not incidental: `mypy --strict` treats an *imported* name as
# module-private unless re-exported explicitly (`--no-implicit-reexport`,
# implied by `strict = true`) — a name this module *defines* itself (like
# `SupervisedProcess` below) needs no such declaration, but `ProcessHandle`/
# `Spawner`/`Sleeper`/`spawn_subprocess` are imports as of issue #74's move to
# `ansina.process.handles`, so `tests/unit/dev/test_sidecar.py`'s own
# `from ansina.dev.sidecar import ProcessHandle` needs this to keep resolving.
__all__ = [
    "ProcessHandle",
    "Sleeper",
    "Spawner",
    "SupervisedProcess",
    "spawn_subprocess",
]

# A private alias, not a new identity — `SupervisedProcess.__init__`'s own
# `spawner: Spawner = _spawn_subprocess` default, and
# `tests/unit/dev/test_sidecar.py`'s own `from ansina.dev.sidecar import
# _spawn_subprocess`, both predate issue #74's move to `ansina.process.handles`
# and are unaffected by it.
_spawn_subprocess = spawn_subprocess


class SupervisedProcess:
    """One supervised background child: a bounded-retry `start()`, a one-shot
    post-spawn liveness check, and a terminate-then-kill `stop()` — the whole of
    what issue #62 asks for ("no continuous supervision loop ... at most one
    bounded restart attempt if the initial spawn fails").
    """

    def __init__(
        self,
        *,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: Path | None = None,
        liveness_delay_seconds: float,
        shutdown_timeout_seconds: float,
        spawner: Spawner = _spawn_subprocess,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        self._argv = tuple(argv)
        self._env = dict(env)
        self._cwd = cwd
        self._liveness_delay_seconds = liveness_delay_seconds
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._spawner = spawner
        self._sleeper = sleeper
        self._handle: ProcessHandle | None = None

    @property
    def pid(self) -> int | None:
        return self._handle.pid if self._handle is not None else None

    @property
    def argv(self) -> tuple[str, ...]:
        """Read-only — lets a caller (or a test) see exactly what would be/was
        spawned, without reaching into a private attribute.
        """
        return self._argv

    def is_alive(self) -> bool:
        return self._handle is not None and self._handle.poll() is None

    def start(self) -> bool:
        """Spawns the child, waits `liveness_delay_seconds`, then checks it's still
        alive. A spawn `OSError` or an immediate death gets exactly one bounded
        retry; a second failure is a loud log and `False` — never an unbounded
        loop, never a raise (a failed spawn must never affect the daemon's own
        health).
        """
        for attempt in (1, 2):
            handle = self._try_spawn(attempt)
            if handle is None:
                continue
            self._sleeper(self._liveness_delay_seconds)
            exit_code = handle.poll()
            if exit_code is None:
                self._handle = handle
                logger.info(
                    "dev mode: vector sidecar started",
                    extra={"pid": handle.pid, "attempt": attempt},
                )
                return True
            logger.warning(
                "dev mode: vector sidecar died immediately after spawn",
                extra={"attempt": attempt, "exit_code": exit_code},
            )
        logger.error(
            "dev mode: vector sidecar failed to start after one retry, giving up"
        )
        return False

    def _try_spawn(self, attempt: int) -> ProcessHandle | None:
        try:
            return self._spawner(self._argv, env=self._env, cwd=self._cwd)
        except OSError:
            logger.exception(
                "dev mode: failed to spawn vector sidecar", extra={"attempt": attempt}
            )
            return None

    def stop(self) -> None:
        """Idempotent: `terminate()` -> bounded wait -> `kill()`. Safe to call with
        no child ever having started, or twice in a row — `stop()` never raises.
        """
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            handle.terminate()
            handle.wait(timeout=self._shutdown_timeout_seconds)
        except subprocess.TimeoutExpired:
            logger.warning(
                "dev mode: vector sidecar did not exit in time, killing",
                extra={"pid": handle.pid},
            )
            self._force_kill(handle)
        except ProcessLookupError, OSError:
            # Already dead — a `terminate()`/`wait()` against a reaped or missing
            # process can raise rather than quietly no-op, depending on
            # platform/timing. Nothing left to do either way.
            pass
        else:
            logger.info("dev mode: vector sidecar stopped", extra={"pid": handle.pid})

    def _force_kill(self, handle: ProcessHandle) -> None:
        try:
            handle.kill()
            handle.wait()
        except ProcessLookupError, OSError:
            pass
