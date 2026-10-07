"""`SpooledRotatingWriter` — a size/age/cap-bounded rotating file writer. See issue
#61.

Shared by the sample producer (`ansina.telemetry.sampler`) and the log-mirror
handler (`ansina.telemetry.log_mirror`) — both need the identical three-way budget
(rotate at a per-file size, cap total bytes for the family, sweep by age), so the
logic lives once here rather than twice. Each *family* (one `file_prefix` — e.g.
`"samples"` or `"log"`) is bounded independently: this writer never looks at any
other family's files, so two families sharing one `spool_dir` never interact.

Stateless and restart-safe by design: nothing is cached across calls or across
process restarts. The active file is always `<spool_dir>/<file_prefix>.jsonl` — its
own existence and size on disk *is* the state, derived fresh on every
`write_line()` call rather than tracked in memory, so a rotated process picks up
exactly where the file on disk left off.

Two independent sweep bounds, not one, the same reasoning
`auth.repositories.LoginAttemptRepository.delete_expired` already documents for
`login_attempts`: nothing guarantees `retention_hours` is the tighter bound or
`max_spool_bytes` is — a file failing either test is deleted. The active file is
never deleted by either sweep, even if that means briefly exceeding
`max_spool_bytes` while it fills — only a *rotated* (closed) file is ever removed.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path


class SpooledRotatingWriter:
    """Appends lines to `<spool_dir>/<file_prefix>.jsonl`, rotating at
    `max_file_bytes` and sweeping the family's rotated files by
    `retention_hours`/`max_spool_bytes` on every write.

    `clock` is injectable (defaults to `time.time`) — both the rotated-file naming
    suffix and the retention cutoff are computed from it, so the unit suite never
    needs real sleeping.
    """

    def __init__(
        self,
        *,
        spool_dir: Path,
        file_prefix: str,
        max_file_bytes: int,
        max_spool_bytes: int,
        retention_hours: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._spool_dir = spool_dir
        self._file_prefix = file_prefix
        self._max_file_bytes = max_file_bytes
        self._max_spool_bytes = max_spool_bytes
        self._retention_seconds = retention_hours * 3600.0
        self._clock = clock

    @property
    def active_path(self) -> Path:
        """The file every `write_line()` call appends to — never deleted by this
        writer's own budget enforcement, regardless of age or size.
        """
        return self._spool_dir / f"{self._file_prefix}.jsonl"

    def write_line(self, line: str) -> None:
        """Appends `line` (plus a trailing newline) to the active file, rotating
        first if the active file is already at `max_file_bytes`, then sweeps the
        family's budget. Creates `spool_dir` on first use — never ahead of time.
        """
        self._spool_dir.mkdir(parents=True, exist_ok=True)
        active = self.active_path
        if active.exists() and active.stat().st_size >= self._max_file_bytes:
            self._rotate(active)
        with active.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        self._enforce_budget()

    def _rotate(self, active: Path) -> None:
        """Renames the active file out of the way so the next write starts a fresh
        one. Guards against a same-millisecond collision (two rotations within the
        clock's own resolution) by appending an incrementing counter.
        """
        suffix = self._rotation_suffix()
        target = self._spool_dir / f"{self._file_prefix}-{suffix}.jsonl"
        counter = 0
        while target.exists():
            counter += 1
            target = (
                self._spool_dir
                / f"{self._file_prefix}-{self._rotation_suffix()}-{counter}.jsonl"
            )
        active.rename(target)

    def _rotation_suffix(self) -> str:
        return f"{int(self._clock() * 1000):013d}"

    def _family_files(self) -> list[Path]:
        return [
            path
            for path in self._spool_dir.glob(f"{self._file_prefix}*.jsonl")
            if path.is_file()
        ]

    def _enforce_budget(self) -> None:
        """Age sweep first, then the total-bytes cap — the two independent cutoffs
        `TelemetrySettings.retention_hours`'s own docstring already names. Any
        `OSError` here (e.g. a concurrent cleanup racing this same spool_dir) is
        deliberately left to propagate rather than caught locally: every real
        caller of `write_line()` (`TelemetrySampler`/`TelemetryLogHandler`) already
        wraps it in its own swallow-and-log boundary, the same "fault isolation at
        the boundary, not scattered through the implementation" shape
        `TickLoop.run()` already uses for a whole tick.
        """
        active = self.active_path
        now = self._clock()
        cutoff = now - self._retention_seconds

        survivors: list[Path] = []
        for path in self._family_files():
            if path == active:
                survivors.append(path)
                continue
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
            else:
                survivors.append(path)

        survivors.sort(key=lambda path: path.stat().st_mtime)
        total = sum(path.stat().st_size for path in survivors)
        for path in survivors:
            if total <= self._max_spool_bytes:
                break
            if path == active:
                continue
            total -= path.stat().st_size
            path.unlink(missing_ok=True)
