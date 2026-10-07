"""Dev Mode: `ansina --dev`'s own posture — a lab/pre-customer configuration, never
a default. See issue #62.

Established in `__main__.py`, before uvicorn binds a port — explicitly not inside
`create_app()` or its ASGI lifespan, so the app factory, the unit suite, and
`tests/e2e` (which launch `python -m ansina` as a subprocess with no `--dev` flag)
see zero change.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import FrameType

from ansina.config.settings import Settings
from ansina.dev.sidecar import SupervisedProcess
from ansina.dev.vector import build_vector_sidecar
from ansina.logging import get_logger

logger = get_logger(__name__)

# The exact two signals uvicorn's own `Server.capture_signals()` handles
# (`uvicorn/server.py`'s `HANDLED_SIGNALS`) — see `_install_shutdown_hook`'s own
# docstring for why this module needs the identical set.
_SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM)

SignalHandler = Callable[[int, FrameType | None], None]


def _install_shutdown_hook(sidecar: SupervisedProcess) -> Callable[[], None]:
    """Stops `sidecar` the moment the daemon is *actually* about to die from
    SIGINT/SIGTERM — not via a `try`/`finally` around `uvicorn.run()`, which
    never runs on that path. Returns an `uninstall()` callable for the non-signal
    exit path.

    **Why a plain `finally` around `uvicorn.run()` cannot work** (found during
    this issue's own Mac-independent Linux verification run, confirmed by
    reading `uvicorn`'s own source, not guessed): `uvicorn.Server.capture_signals()`
    installs its own handler for the *duration* of serving, and on its way out —
    after its own graceful HTTP shutdown has fully finished — it restores
    whatever handler was active *before* it ran and then calls
    `signal.raise_signal()` to redeliver the *same* signal, "to trigger the
    expected behaviour" (`uvicorn/server.py`'s own comment). With no handler
    installed ahead of `uvicorn.run()`, that default disposition is "terminate
    the process" — which it does, synchronously, from inside `Server.run()`,
    so `uvicorn.run()` never returns to its caller and no code after it (a
    `finally`'s body included) ever executes. Confirmed by direct observation:
    a real `ansina --dev` process sent a real SIGTERM completes uvicorn's own
    full shutdown log sequence and then simply ceases to exist — the Vector
    child it had spawned was left running, orphaned, every time.

    The fix installs a handler for the same two signals *before* `uvicorn.run()`
    is ever called. `capture_signals()` then captures *this* handler as the one
    to restore and replay through — so when it redelivers the signal after its
    own shutdown completes, it's this handler that runs, not the OS default.
    This handler stops the sidecar, puts the original (pre-`dev_mode`)
    disposition back, and redelivers the signal once more via `os.kill` — the
    same restore-then-replay idiom `capture_signals()` itself already uses — so
    the process still exits with the exact same signal-termination semantics a
    caller (a shell, `tests/e2e`'s own `process.returncode == -signal.SIGTERM`
    assertion, a process supervisor) already expects; this module only inserts
    one cleanup step ahead of that, it doesn't change the outcome.
    """
    previous: dict[int, SignalHandler | int | None] = {}

    def _stop_then_redeliver(sig: int, frame: FrameType | None) -> None:
        sidecar.stop()
        signal.signal(sig, previous[sig])
        os.kill(os.getpid(), sig)

    for sig in _SHUTDOWN_SIGNALS:
        previous[sig] = signal.signal(sig, _stop_then_redeliver)

    def _uninstall() -> None:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    return _uninstall


@contextmanager
def dev_mode(settings: Settings) -> Iterator[None]:
    """`settings.dev.enabled` false (the default) — yields immediately, logging and
    creating nothing, a byte-for-byte no-op. True — logs an unmistakable boot
    banner (a `logger.warning`, not a `DEBUG`-level line, so it is never mistaken
    for the default posture), preflights + spawns the Vector sidecar, installs
    the signal hook `_install_shutdown_hook` documents, yields, and tears both
    down — the signal hook on a real SIGINT/SIGTERM (the expected shutdown path:
    see that function's own docstring for why a `finally` alone can't do this),
    the `finally` below as the backstop for every other exit (an exception
    raised from inside the `with` block, or any future caller that doesn't go
    through a real OS signal at all). `SupervisedProcess.stop()` is idempotent,
    so running both is harmless. A spawn failure, crash, or mid-run death has no
    effect on the daemon either side of the `yield` — telemetry is best-effort by
    construction, never a dependency the daemon's own health depends on.

    A SIGKILL (as opposed to SIGINT/SIGTERM) to the daemon bypasses all of this
    and can still orphan the Vector child — nothing can run in response to a
    signal that never invokes a handler at all. `SupervisedProcess`'s own
    `start_new_session=True` is what makes an orderly shutdown *possible* (the
    child sits outside the daemon's signal group, so it is terminated
    deliberately here rather than hit uncontrolled by the same signal), not a
    guarantee against every possible way the daemon's process can die.
    """
    if not settings.dev.enabled:
        yield
        return

    telemetry = settings.telemetry
    logger.warning(
        "DEV MODE ENABLED — lab/pre-customer posture, never use this in production",
        extra={
            "dev_mode": True,
            "spool_dir": str(telemetry.spool_dir),
            "bucket": telemetry.s3.bucket,
            "vector_config": str(settings.dev.vector_config),
        },
    )
    sidecar = build_vector_sidecar(settings)
    if sidecar is not None:
        sidecar.start()

    uninstall = _install_shutdown_hook(sidecar) if sidecar is not None else None
    try:
        yield
    finally:
        if uninstall is not None:
            uninstall()
        if sidecar is not None:
            sidecar.stop()
