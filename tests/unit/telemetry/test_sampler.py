from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ansina.config import load_settings
from ansina.heart.tick.decision import TickDecision
from ansina.telemetry.rotation import SpooledRotatingWriter
from ansina.telemetry.sampler import (
    TelemetrySample,
    TelemetrySampler,
    _read_peak_rss_kib,
    build_telemetry_sampler,
    collect_sample,
)


class _FakeTick:
    """A `TickController`-shaped double. `collect_sample` only ever reads the
    properties; `pause`/`resume`/`is_healthy`/`start`/`stop` exist purely so this
    class keeps satisfying the full structural Protocol.
    """

    def __init__(
        self,
        *,
        ticks_run: int = 7,
        paused: bool = False,
        pause_reason: str | None = None,
        last_decision: TickDecision | None = TickDecision.IDLE,
        last_tick_at: str | None = None,
        last_duration_seconds: float | None = 0.05,
        failures_total: int = 1,
        consecutive_failures: int = 0,
        consecutive_overruns: int = 0,
    ) -> None:
        self.ticks_run = ticks_run
        self.paused = paused
        self.pause_reason = pause_reason
        self.last_decision = last_decision
        self.last_tick_at = last_tick_at
        self.last_duration_seconds = last_duration_seconds
        self.failures_total = failures_total
        self.consecutive_failures = consecutive_failures
        self.consecutive_overruns = consecutive_overruns

    def pause(self, *, reason: str | None = None) -> None:
        pass

    def resume(self) -> None:
        pass

    def is_healthy(self) -> bool:
        return True

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class _FakeRusage:
    def __init__(self, ru_maxrss: int) -> None:
        self.ru_maxrss = ru_maxrss


def test_read_peak_rss_kib_leaves_rss_unscaled_on_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ru_maxrss` is already bytes on macOS/BSD — forced via monkeypatch so this
    holds regardless of which OS actually runs the suite, the same reasoning
    `tests/unit/heart/eval/test_runner.py`'s own `test_run_bench_leaves_rss_
    unscaled_on_darwin` documents (and the same real CI gap that test exists to
    close: without forcing both branches, the real macOS CI leg never executes
    the Linux-only `*= 1024` line, and the real Linux leg never proves the
    darwin-unscaled path either).
    """
    monkeypatch.setattr("ansina.telemetry.sampler.sys.platform", "darwin")
    monkeypatch.setattr(
        "ansina.telemetry.sampler.resource.getrusage",
        lambda _who: _FakeRusage(2048 * 1024),
    )

    assert _read_peak_rss_kib() == 2048


def test_read_peak_rss_kib_scales_from_kib_to_bytes_off_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ru_maxrss` is KiB on Linux — forced via monkeypatch for the same reason as
    the darwin case above.
    """
    monkeypatch.setattr("ansina.telemetry.sampler.sys.platform", "linux")
    monkeypatch.setattr(
        "ansina.telemetry.sampler.resource.getrusage",
        lambda _who: _FakeRusage(2048),
    )

    assert _read_peak_rss_kib() == 2048


def test_read_peak_rss_kib_returns_none_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import resource

    def _broken_getrusage(_who: int) -> object:
        raise ValueError("simulated failure")

    monkeypatch.setattr(resource, "getrusage", _broken_getrusage)

    assert _read_peak_rss_kib() is None


def test_telemetry_sample_to_json_line_round_trips() -> None:
    sample = TelemetrySample(
        t=1.5,
        elapsed_s=10,
        rss_kib=2048,
        ticks=3,
        paused=False,
        paused_reason=None,
        last_decision="idle",
        last_duration_seconds=0.1,
        failures_total=0,
        consecutive_failures=0,
        consecutive_overruns=0,
    )

    line = sample.to_json_line()

    import json

    assert json.loads(line) == {
        "t": 1.5,
        "elapsed_s": 10,
        "rss_kib": 2048,
        "ticks": 3,
        "paused": False,
        "paused_reason": None,
        "last_decision": "idle",
        "last_duration_seconds": 0.1,
        "failures_total": 0,
        "consecutive_failures": 0,
        "consecutive_overruns": 0,
    }


def test_collect_sample_with_no_tick_renders_every_tick_field_null() -> None:
    sample = collect_sample(now=100.0, elapsed_s=5, rss_kib=1234, tick=None)

    assert sample.t == 100.0
    assert sample.elapsed_s == 5
    assert sample.rss_kib == 1234
    assert sample.ticks is None
    assert sample.paused is None
    assert sample.paused_reason is None
    assert sample.last_decision is None
    assert sample.last_duration_seconds is None
    assert sample.failures_total is None
    assert sample.consecutive_failures is None
    assert sample.consecutive_overruns is None


def test_collect_sample_with_a_tick_reads_every_field_through() -> None:
    tick = _FakeTick(
        ticks_run=42,
        paused=True,
        pause_reason="manual",
        last_decision=TickDecision.ESCALATE,
        last_duration_seconds=1.23,
        failures_total=2,
        consecutive_failures=1,
        consecutive_overruns=3,
    )

    sample = collect_sample(now=200.0, elapsed_s=60, rss_kib=5555, tick=tick)

    assert sample.ticks == 42
    assert sample.paused is True
    assert sample.paused_reason == "manual"
    assert sample.last_decision == "escalate"
    assert sample.last_duration_seconds == 1.23
    assert sample.failures_total == 2
    assert sample.consecutive_failures == 1
    assert sample.consecutive_overruns == 3


def test_collect_sample_with_a_tick_and_no_decision_yet() -> None:
    tick = _FakeTick(last_decision=None)

    sample = collect_sample(now=0.0, elapsed_s=0, rss_kib=None, tick=tick)

    assert sample.last_decision is None


def test_rejects_non_positive_interval(tmp_path: Path) -> None:
    writer = SpooledRotatingWriter(
        spool_dir=tmp_path,
        file_prefix="samples",
        max_file_bytes=1_000_000,
        max_spool_bytes=1_000_000,
        retention_hours=24.0,
    )

    with pytest.raises(ValueError, match="interval_seconds must be > 0"):
        TelemetrySampler(writer=writer, interval_seconds=0, tick=None)


# --- TelemetrySampler lifecycle -------------------------------------------------------


async def test_is_healthy_reflects_task_lifecycle(tmp_path: Path) -> None:
    writer = SpooledRotatingWriter(
        spool_dir=tmp_path,
        file_prefix="samples",
        max_file_bytes=1_000_000,
        max_spool_bytes=1_000_000,
        retention_hours=24.0,
    )
    sampler = TelemetrySampler(writer=writer, interval_seconds=100, tick=None)

    assert not sampler.is_healthy()

    await sampler.start()
    assert sampler.is_healthy()

    await sampler.stop()
    assert not sampler.is_healthy()


async def test_stop_before_start_does_not_hang(tmp_path: Path) -> None:
    writer = SpooledRotatingWriter(
        spool_dir=tmp_path,
        file_prefix="samples",
        max_file_bytes=1_000_000,
        max_spool_bytes=1_000_000,
        retention_hours=24.0,
    )
    sampler = TelemetrySampler(writer=writer, interval_seconds=100, tick=None)

    await asyncio.wait_for(sampler.stop(), timeout=1)


async def test_stop_ends_a_long_wait_promptly(tmp_path: Path) -> None:
    writer = SpooledRotatingWriter(
        spool_dir=tmp_path,
        file_prefix="samples",
        max_file_bytes=1_000_000,
        max_spool_bytes=1_000_000,
        retention_hours=24.0,
    )
    sampler = TelemetrySampler(writer=writer, interval_seconds=100, tick=None)
    await sampler.start()

    await asyncio.wait_for(sampler.stop(), timeout=1)


async def test_sampler_writes_one_json_line_per_interval(tmp_path: Path) -> None:
    writer = SpooledRotatingWriter(
        spool_dir=tmp_path,
        file_prefix="samples",
        max_file_bytes=1_000_000,
        max_spool_bytes=1_000_000,
        retention_hours=24.0,
    )
    tick = _FakeTick()
    sampler = TelemetrySampler(
        writer=writer,
        interval_seconds=0.02,
        tick=tick,
        rss_reader=lambda: 999,
    )

    await sampler.start()
    await asyncio.sleep(0.09)
    await sampler.stop()

    lines = writer.active_path.read_text().splitlines()
    assert len(lines) >= 2
    import json

    first = json.loads(lines[0])
    assert first["rss_kib"] == 999
    assert first["ticks"] == 7
    assert first["last_decision"] == "idle"


async def test_a_failing_sample_is_logged_and_the_loop_continues(
    tmp_path: Path, captured_logs: Callable[[], list[dict[str, Any]]]
) -> None:
    writer = SpooledRotatingWriter(
        spool_dir=tmp_path,
        file_prefix="samples",
        max_file_bytes=1_000_000,
        max_spool_bytes=1_000_000,
        retention_hours=24.0,
    )
    calls = {"count": 0}

    def _flaky_rss() -> int | None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated rss read failure")
        return 111

    sampler = TelemetrySampler(
        writer=writer,
        interval_seconds=0.02,
        tick=None,
        rss_reader=_flaky_rss,
    )

    await sampler.start()
    await asyncio.sleep(0.09)
    await sampler.stop()

    assert any(line["level"] == "ERROR" for line in captured_logs())
    assert writer.active_path.exists()
    assert len(writer.active_path.read_text().splitlines()) >= 1


def test_build_telemetry_sampler_wires_settings_into_the_writer_and_sampler(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ANSINA_TELEMETRY__ENABLED", "true")
    monkeypatch.setenv("ANSINA_TELEMETRY__SPOOL_DIR", str(tmp_path))
    monkeypatch.setenv("ANSINA_TELEMETRY__SAMPLE_INTERVAL_SECONDS", "12.5")
    monkeypatch.setenv("ANSINA_TELEMETRY__MAX_FILE_BYTES", "2048")
    monkeypatch.setenv("ANSINA_TELEMETRY__MAX_SPOOL_BYTES", "4096")
    monkeypatch.setenv("ANSINA_TELEMETRY__RETENTION_HOURS", "48")
    settings = load_settings()
    tick = _FakeTick()

    sampler = build_telemetry_sampler(settings, tick=tick)

    assert isinstance(sampler, TelemetrySampler)
    assert sampler._interval == 12.5
    assert sampler._tick is tick
    assert sampler._writer._spool_dir == tmp_path
    assert sampler._writer._file_prefix == "samples"
    assert sampler._writer._max_file_bytes == 2048
    assert sampler._writer._max_spool_bytes == 4096
    assert sampler._writer._retention_seconds == 48 * 3600.0


def test_build_telemetry_sampler_accepts_no_tick(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ANSINA_TELEMETRY__ENABLED", "true")
    monkeypatch.setenv("ANSINA_TELEMETRY__SPOOL_DIR", str(tmp_path))
    settings = load_settings()

    sampler = build_telemetry_sampler(settings, tick=None)

    assert isinstance(sampler, TelemetrySampler)
    assert sampler._tick is None
