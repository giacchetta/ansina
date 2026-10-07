"""`TelemetrySampler` — the flag-gated RSS/tick/decision sample producer. See issue
#61.

Reuses `scripts/heart-soak-run.sh`'s own sample schema verbatim (`t`, `elapsed_s`,
`rss_kib`, `ticks`, `paused`, `paused_reason`, `last_decision`,
`last_duration_seconds` — see `docs/heart/soak.md`), extended with the three
circuit-breaker counters `GET /heart/tick` does not expose (M6's own
`docs/heart/findings.md` open question #2): `failures_total`,
`consecutive_failures`, `consecutive_overruns`.

**`rss_kib` is peak, not instantaneous, RSS — a deliberate divergence from the
soak script.** `scripts/heart-soak-run.sh` shells out to `ps -o rss=` for an
instantaneous reading, but `heart/eval/provenance.py`'s own module docstring states
a real invariant this codebase already relies on: the daemon itself never shells
out (subprocess usage stays confined to dev-tooling under `heart/eval/`). Spawning
a process every `sample_interval_seconds` inside an always-on daemon is also a real
operational cost with no payoff proportional to it. This module instead reads
`resource.getrusage(RUSAGE_SELF).ru_maxrss` — the same call, with the same
platform-dependent byte/KiB correction, `heart/eval/runner.py` already uses for its
own `peak_rss_bytes` metric (duplicated locally here rather than imported:
`ansina.heart.eval` is dev-tooling and must stay unimported by the daemon's runtime
graph). The trade-off, named here rather than discovered later: `ru_maxrss` is a
*peak*, monotonically non-decreasing for the process's whole lifetime, not an
instantaneous reading — a flattening plateau over time still reads as "no leak" and
a climbing curve still reads as "leak" (the actual question a growth-trend review
asks), but the two data sources are not bit-comparable sample-for-sample.

Sampling runs independently of `[heart]`/`[heart.tick]` — an operator may want
RSS/uptime telemetry on a daemon with the Heart disabled. `tick` is `None` in that
case, and every tick-derived field renders `null` rather than raising.
"""

from __future__ import annotations

import asyncio
import functools
import resource
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from json import dumps
from typing import Protocol

import anyio.to_thread

from ansina.config.settings import Settings
from ansina.heart.tick.loop import TickController
from ansina.logging import get_logger
from ansina.telemetry.rotation import SpooledRotatingWriter

logger = get_logger(__name__)

Clock = Callable[[], float]
RssReader = Callable[[], int | None]


def _read_peak_rss_kib() -> int | None:
    """Peak RSS in KiB for this process — see the module docstring for why this,
    not an instantaneous `ps`-based reading, is what the daemon itself can safely
    read. `ru_maxrss` is bytes on macOS/BSD and KiB on Linux — a `str`-typed local,
    not a direct `sys.platform ==` comparison, for the same mypy-unreachable-branch
    reason `heart/selection.py`'s `_mlx_viable` and `heart/eval/runner.py`'s
    `run_bench` already document (CI runs both `ubuntu-24.04` and `macos-26`, so a
    direct comparison would statically strand one leg's branch). Never raises —
    `resource.getrusage` can't meaningfully fail for `RUSAGE_SELF`, but this stays
    defensive the same way every other best-effort telemetry read in this module
    does.
    """
    try:
        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except OSError, ValueError:
        return None
    current_platform: str = sys.platform
    if current_platform != "darwin":
        peak_rss *= 1024
    return int(peak_rss // 1024)


@dataclass(frozen=True, slots=True)
class TelemetrySample:
    """One sample — the exact field set `scripts/heart-soak-run.sh` writes to
    `samples.jsonl`, in the same order, plus the three breaker counters it could
    only read indirectly (via `paused`/`paused_reason`).
    """

    t: float
    elapsed_s: int
    rss_kib: int | None
    ticks: int | None
    paused: bool | None
    paused_reason: str | None
    last_decision: str | None
    last_duration_seconds: float | None
    failures_total: int | None
    consecutive_failures: int | None
    consecutive_overruns: int | None

    def to_json_line(self) -> str:
        return dumps(asdict(self), separators=(",", ":"))


def collect_sample(
    *,
    now: float,
    elapsed_s: int,
    rss_kib: int | None,
    tick: TickController | None,
) -> TelemetrySample:
    """Pure: builds one `TelemetrySample` from already-read inputs. `tick is None`
    (the Heart/tick loop disabled) renders every tick-derived field `None` rather
    than raising — telemetry must stay useful (RSS/uptime) even with the Heart off.
    """
    if tick is None:
        return TelemetrySample(
            t=now,
            elapsed_s=elapsed_s,
            rss_kib=rss_kib,
            ticks=None,
            paused=None,
            paused_reason=None,
            last_decision=None,
            last_duration_seconds=None,
            failures_total=None,
            consecutive_failures=None,
            consecutive_overruns=None,
        )
    last_decision = tick.last_decision
    return TelemetrySample(
        t=now,
        elapsed_s=elapsed_s,
        rss_kib=rss_kib,
        ticks=tick.ticks_run,
        paused=tick.paused,
        paused_reason=tick.pause_reason,
        last_decision=last_decision.value if last_decision is not None else None,
        last_duration_seconds=tick.last_duration_seconds,
        failures_total=tick.failures_total,
        consecutive_failures=tick.consecutive_failures,
        consecutive_overruns=tick.consecutive_overruns,
    )


class TelemetrySamplerLifecycle(Protocol):
    """The minimal surface `create_app`'s lifespan needs — structural, like
    `heart.tick.loop.TickLifecycle`, so a lifespan-ordering test can inject a
    lightweight double instead of a real `TelemetrySampler`.
    """

    def is_healthy(self) -> bool: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class TelemetrySamplerFactory(Protocol):
    """The shape of `build_telemetry_sampler` (and any test double standing in for
    it) — `create_app`'s `telemetry_factory` parameter.
    """

    def __call__(
        self, settings: Settings, /, *, tick: TickController | None
    ) -> TelemetrySamplerLifecycle: ...


class TelemetrySampler:
    """Every `sample_interval_seconds`, reads RSS + the tick loop's live state and
    appends one JSON line to the `samples` family in `spool_dir` — the sample
    producer's own periodic loop, shaped like `heart.tick.loop.TickLoop`'s: an
    `asyncio.Event`-gated wait so `stop()` can interrupt a pending wait
    immediately, and a swallow-and-log `try/except` around each sample so one
    failed read or write can never end the loop.
    """

    def __init__(
        self,
        *,
        writer: SpooledRotatingWriter,
        interval_seconds: float,
        tick: TickController | None,
        rss_reader: RssReader = _read_peak_rss_kib,
        clock: Clock = time.monotonic,
        wall_clock: Clock = time.time,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        self._writer = writer
        self._interval = interval_seconds
        self._tick = tick
        self._rss_reader = rss_reader
        self._clock = clock
        self._wall_clock = wall_clock
        self._started_at = clock()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def is_healthy(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            if await self._wait_or_stop(self._interval):
                break
            try:
                await self._sample_once()
            except Exception:
                logger.exception("telemetry sample: unhandled error, continuing")

    async def _wait_or_stop(self, delay: float) -> bool:
        """Waits up to `delay` seconds, returning `True` early iff `stop()` fired —
        the same shape `TickLoop._wait_or_stop` already uses.
        """
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            return True
        except TimeoutError:
            return False

    async def _sample_once(self) -> None:
        rss_kib = await anyio.to_thread.run_sync(self._rss_reader)
        sample = collect_sample(
            now=self._wall_clock(),
            elapsed_s=int(self._clock() - self._started_at),
            rss_kib=rss_kib,
            tick=self._tick,
        )
        await anyio.to_thread.run_sync(
            functools.partial(self._writer.write_line, sample.to_json_line())
        )


def build_telemetry_sampler(
    settings: Settings, /, *, tick: TickController | None
) -> TelemetrySamplerLifecycle:
    """The default `telemetry_factory` for `create_app` — only ever called when
    `settings.telemetry.enabled`, mirroring `build_heart_runtime`/
    `build_brain_provider`'s own "gated by the caller, not internally" shape.
    Never raises: a bad `spool_dir` only ever fails an individual write (logged and
    swallowed inside `TelemetrySampler._run`), never boot — the same "best-effort,
    never a boot-time failure" posture `[telemetry.s3]`'s own `build_report_storage`
    documents for upload failures.
    """
    telemetry_settings = settings.telemetry
    writer = SpooledRotatingWriter(
        spool_dir=telemetry_settings.spool_dir,
        file_prefix="samples",
        max_file_bytes=telemetry_settings.max_file_bytes,
        max_spool_bytes=telemetry_settings.max_spool_bytes,
        retention_hours=telemetry_settings.retention_hours,
    )
    return TelemetrySampler(
        writer=writer,
        interval_seconds=telemetry_settings.sample_interval_seconds,
        tick=tick,
    )
