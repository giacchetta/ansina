"""`AgentRunner` — the port every "Hands" executor adapter implements — and
`OpencodeCliRunner`, the one adapter: a one-shot `opencode run --format json`
child per call. See issue #75.

The load-bearing invariant, borrowed from `BrainProvider.stream()`
(`ansina.brain.provider`, itself borrowed from OpenClaw's `ApiProvider` —
`docs/architecture/blueprint.md` §1): once a run is genuinely under way (a
subprocess has been spawned, or its spawn has been attempted), `run()` never
raises — `asyncio.CancelledError` is the one exception let through untouched,
the same `BaseBrainProvider._run` discipline. Every other failure (a spawn
`OSError`, a non-zero exit, a timeout, an output-size cap) resolves into a
terminal `RunOutcome` instead.

Unlike Brain, there is no separate "construction/selection time" factory in
this issue that can raise before `run()` is ever called — `queue_max_depth`
is inherently a per-call property (how many calls are *currently* admitted),
not a construction-time one. So the three narrow exceptions to the
never-raises rule all happen at the very top of `run()`, before any
subprocess exists — an *admission-control* phase, not a run outcome:
`OpencodeUnavailableError` (this runner has no resolved `opencode_path`),
`OpencodeRunFailedError` (the request itself is rejected, e.g. an
oversized prompt), and `OpencodeBusyError` (`[opencode] queue_max_depth`
would be exceeded) — the one case issue #75's own acceptance criteria
describe as refused "at the port."

This module is also where issue #75's architecture amendment actually lands
in code: `OpencodeCliRunner.run()` is the first repeated, request-triggered
subprocess spawn anywhere in this codebase — see `ansina.heart.eval.
provenance`'s module docstring (updated by this issue) and `docs/
architecture/blueprint.md` §2/§3 for the full statement of the widened "the
daemon never shells out" invariant, confined to `ansina.opencode` alone.
"""

from __future__ import annotations

import asyncio
import functools
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Protocol, runtime_checkable

import anyio.to_thread

from ansina.config.settings import Settings
from ansina.errors import (
    OpencodeBusyError,
    OpencodeRunFailedError,
    OpencodeUnavailableError,
)
from ansina.logging import get_logger
from ansina.opencode.env import build_opencode_env
from ansina.opencode.events import (
    RunEvent,
    RunOutcome,
    RunRequest,
    RunStatus,
    parse_run_event,
)
from ansina.process.handles import (
    PipedProcessHandle,
    PipedSpawner,
    spawn_piped_subprocess,
)

logger = get_logger(__name__)

OnEvent = Callable[[RunEvent], None]


@runtime_checkable
class AgentRunner(Protocol):
    """The port. Structural, like `HeartRuntime`/`BrainProvider` — speaks only
    in `RunRequest`/`RunEvent`/`RunOutcome`, never argv/pids/stdout, so a
    future `opencode serve` + httpx adapter (a persistent server, polled or
    SSE-driven) is a pure append with no change to a caller of this Protocol.
    """

    async def run(self, request: RunRequest, on_event: OnEvent) -> RunOutcome:
        """Never raises once a subprocess exists or its spawn was attempted —
        see module docstring for the narrow admission-control exceptions
        that may raise before that point, and for the one exception
        (`asyncio.CancelledError`) let through at any point.

        `on_event` is called synchronously, from this coroutine, once per
        classified line, as each line arrives — never batched until exit,
        and never called from any thread other than the one `run()` itself
        is running on.
        """
        ...


class OpencodeCliRunner:
    """The one `AgentRunner` adapter: `opencode run <prompt> --format json
    ...` via `ansina.process.handles.PipedSpawner`, one child process per
    `run()` call. The child's own working directory — `cwd=
    opencode.workspace_dir`, passed at the real spawn call in `_run_one()` —
    is what confines it to the workspace; `opencode run` has no directory
    flag at all (verified against a real `opencode v2.1.4` binary — see
    issue #81, which removed a `--dir` flag `_build_argv()` used to pass
    that the real CLI rejects outright).

    Concurrency and backlog are enforced per instance, not per Protocol —
    `asyncio.Semaphore(max_concurrent_runs)` bounds how many children may
    actually be spawned/running at once; `_admitted` (guarded by
    `_admission_lock`) bounds the total number of calls admitted into `run()`
    at all (waiting on that semaphore, or already running) against
    `queue_max_depth`, checked and incremented *before* the semaphore is ever
    acquired, so a caller that would push the backlog past its own configured
    depth is refused immediately rather than queued unboundedly.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        opencode_path: str | None,
        spawner: PipedSpawner = spawn_piped_subprocess,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._opencode_path = opencode_path
        self._spawner = spawner
        self._clock = clock
        self._semaphore = asyncio.Semaphore(settings.opencode.max_concurrent_runs)
        self._admission_lock = asyncio.Lock()
        self._admitted = 0

    @property
    def opencode_path(self) -> str | None:
        """Read-only — lets a caller (or a test) see exactly which resolved
        binary this runner would spawn, without reaching into a private
        attribute. The same `SupervisedProcess.argv`-style exposure
        (`ansina.dev.sidecar`).
        """
        return self._opencode_path

    async def run(self, request: RunRequest, on_event: OnEvent) -> RunOutcome:
        opencode = self._settings.opencode
        if self._opencode_path is None:
            raise OpencodeUnavailableError(
                "OpencodeCliRunner has no resolved opencode_path — construct "
                "it via ansina.opencode.preflight.build_opencode_service, "
                "which only ever returns a runner on a clean preflight pass"
            )
        if len(request.prompt) > opencode.max_prompt_chars:
            raise OpencodeRunFailedError(
                f"request.prompt is {len(request.prompt)} chars, exceeding "
                f"opencode.max_prompt_chars={opencode.max_prompt_chars}"
            )

        async with self._admission_lock:
            if self._admitted >= opencode.queue_max_depth:
                raise OpencodeBusyError(
                    f"opencode.queue_max_depth={opencode.queue_max_depth} "
                    "would be exceeded by admitting this run"
                )
            self._admitted += 1
        try:
            async with self._semaphore:
                return await self._run_one(request, on_event)
        finally:
            async with self._admission_lock:
                self._admitted -= 1

    def _build_argv(self, request: RunRequest) -> list[str]:
        assert self._opencode_path is not None  # guarded by `run()` above
        opencode = self._settings.opencode
        argv = [
            self._opencode_path,
            "run",
            request.prompt,
            "--format",
            "json",
        ]
        if request.session_id is not None:
            argv += ["--session", request.session_id]
        if request.continue_last:
            argv.append("--continue")
        if request.fork:
            argv.append("--fork")

        model = request.model if request.model is not None else opencode.model
        if model:
            argv += ["--model", model]
        agent = request.agent if request.agent is not None else opencode.agent
        if agent:
            argv += ["--agent", agent]
        auto_approve = (
            request.auto_approve
            if request.auto_approve is not None
            else opencode.auto_approve
        )
        if auto_approve:
            argv.append("--auto")
        return argv

    async def _run_one(self, request: RunRequest, on_event: OnEvent) -> RunOutcome:
        opencode = self._settings.opencode
        argv = self._build_argv(request)
        env = build_opencode_env(self._settings)
        start = self._clock()
        try:
            handle = self._spawner(argv, env=env, cwd=opencode.workspace_dir)
        except OSError as exc:
            logger.warning("opencode run: failed to spawn", extra={"error": str(exc)})
            return RunOutcome(
                status=RunStatus.FAILED,
                exit_code=None,
                events_emitted=0,
                events_skipped=0,
                duration_seconds=self._clock() - start,
                error=f"failed to spawn opencode: {exc}",
            )

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        reader_thread = threading.Thread(
            target=self._read_stdout, args=(handle, loop, queue), daemon=True
        )
        reader_thread.start()

        events_emitted = 0
        events_skipped = 0
        output_bytes = 0
        truncated = False
        timed_out = False
        cancelled = False
        sequence = 0
        try:
            while True:
                remaining = opencode.run_timeout_seconds - (self._clock() - start)
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=remaining)
                except TimeoutError:
                    timed_out = True
                    break
                if line is None:
                    break  # EOF: the child's stdout pipe closed.
                output_bytes += len(line.encode("utf-8"))
                if output_bytes > opencode.max_run_output_bytes:
                    truncated = True
                    break
                event = parse_run_event(
                    line, max_event_bytes=opencode.max_event_bytes, sequence=sequence
                )
                if event is None:
                    events_skipped += 1
                    continue
                sequence += 1
                events_emitted += 1
                on_event(event)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            exit_code = await self._finish(
                handle,
                reader_thread,
                force=timed_out or truncated or cancelled,
                shutdown_timeout_seconds=opencode.shutdown_timeout_seconds,
            )

        duration = self._clock() - start
        if timed_out:
            status = RunStatus.TIMEOUT
            error: str | None = (
                "run exceeded opencode.run_timeout_seconds="
                f"{opencode.run_timeout_seconds}"
            )
        elif truncated:
            status = RunStatus.FAILED
            error = (
                "run output exceeded opencode.max_run_output_bytes="
                f"{opencode.max_run_output_bytes}"
            )
        elif exit_code != 0:
            status = RunStatus.FAILED
            error = f"opencode exited with code {exit_code}"
        else:
            status = RunStatus.COMPLETED
            error = None
        return RunOutcome(
            status=status,
            exit_code=exit_code,
            events_emitted=events_emitted,
            events_skipped=events_skipped,
            duration_seconds=duration,
            truncated=truncated,
            error=error,
        )

    @staticmethod
    def _read_stdout(
        handle: PipedProcessHandle,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[str | None],
    ) -> None:
        """Runs on a dedicated background thread, never the event loop:
        `handle.stdout`'s own line-by-line iteration is a blocking stdlib
        file-object read. Every line is handed to the async side via
        `call_soon_threadsafe` — the only thread-safe way to touch an
        `asyncio.Queue` from outside the event loop — so `on_event` (invoked
        from `_run_one` itself, never from here) can run without any
        assumption about thread-safety. The sentinel `None`, always pushed
        from the `finally`, marks EOF: the child's stdout pipe closing, which
        happens the moment it exits (naturally, or via `terminate()`/
        `kill()`) — this is what lets `_run_one`'s own read loop and this
        thread agree on "nothing more will ever arrive."
        """
        try:
            if handle.stdout is not None:
                for line in handle.stdout:
                    loop.call_soon_threadsafe(queue.put_nowait, line)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    async def _finish(
        self,
        handle: PipedProcessHandle,
        reader_thread: threading.Thread,
        *,
        force: bool,
        shutdown_timeout_seconds: float,
    ) -> int | None:
        """`force` (set on a timeout, an output-cap truncation, or a
        cancellation) is `terminate()` -> a bounded `shutdown_timeout_seconds`
        wait -> `kill()` — issue #75's own acceptance criterion, the same
        terminate-then-kill shape `ansina.dev.sidecar.SupervisedProcess.stop`
        already uses for a long-lived sidecar, here applied to a per-run
        child. A clean EOF (`force=False`) just waits for the exit the pipe
        closing already signaled is imminent.

        `reader_thread.join(...)`, offloaded the same way `handle.wait(...)`
        is (both are blocking stdlib calls), is what makes issue #75's "unit
        test proves the reader thread does not outlive the kill" criterion a
        provable fact rather than an assumption before `run()` returns —
        a killed/terminated/naturally-exited child's own pipe closing is what
        lets that thread's blocking read loop end on its own.
        """
        if force:
            handle.terminate()
            try:
                await anyio.to_thread.run_sync(
                    functools.partial(handle.wait, timeout=shutdown_timeout_seconds)
                )
            except subprocess.TimeoutExpired:
                logger.warning(
                    "opencode run: did not exit after terminate(), killing",
                    extra={"pid": handle.pid},
                )
                handle.kill()
                await anyio.to_thread.run_sync(handle.wait)
        else:
            await anyio.to_thread.run_sync(handle.wait)

        await anyio.to_thread.run_sync(
            functools.partial(reader_thread.join, timeout=shutdown_timeout_seconds)
        )
        if reader_thread.is_alive():
            logger.warning("opencode run: reader thread did not exit in time")
        return handle.poll()
