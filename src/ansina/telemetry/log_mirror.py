"""`TelemetryLogHandler` — mirrors the primary structured log stream to a rotated
file in `[telemetry] spool_dir`. See issue #61.

Wired through `ansina.logging.setup.configure_logging`'s existing `dictConfig` call
as a *second* handler, never a parallel logging path: `dictConfig` instantiates one
`JsonFormatter` per formatter name and shares that single instance across every
handler naming it, so this handler's `self.format(record)` call runs through the
exact same formatter instance (and therefore the exact same redaction) as the
primary `ansina.json` handler — redaction can never be bypassed by forgetting to
wire it twice, because there is only ever one formatter object to wire.

Deliberately has no import on `ansina.heart`/`ansina.logging` itself (only stdlib
`logging` plus `ansina.telemetry.rotation`) — `ansina.logging.setup` imports this
module directly, and `ansina.telemetry`'s own package `__init__` re-exports it, so
keeping this module's own dependency graph minimal is what keeps that import cycle
-free. See `ansina/telemetry/__init__.py`'s module docstring for the full reasoning.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from ansina.telemetry.rotation import SpooledRotatingWriter


class TelemetryLogHandler(logging.Handler):
    """A `logging.Handler` that appends every formatted record to a rotating
    `log.jsonl` family in `spool_dir`, via `SpooledRotatingWriter`.

    Constructed by `logging.config.dictConfig`'s `"()"` callable form directly
    from `[telemetry]` settings values — see `ansina.logging.setup.
    configure_logging`. `clock` is accepted for the same test-injection reason
    `SpooledRotatingWriter` itself takes one; not something `dictConfig` ever
    passes in production.
    """

    def __init__(
        self,
        *,
        spool_dir: str,
        max_file_bytes: int,
        max_spool_bytes: int,
        retention_hours: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__()
        self._writer = SpooledRotatingWriter(
            spool_dir=Path(spool_dir),
            file_prefix="log",
            max_file_bytes=max_file_bytes,
            max_spool_bytes=max_spool_bytes,
            retention_hours=retention_hours,
            clock=clock,
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            self._writer.write_line(line)
        except Exception:
            self.handleError(record)
