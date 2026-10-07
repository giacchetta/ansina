from __future__ import annotations

import time
from pathlib import Path

from ansina.telemetry.rotation import SpooledRotatingWriter


class _FakeClock:
    """A controllable clock — each call advances by `step` unless `set()`/`advance()`
    is used to jump directly, giving deterministic, collision-free rotation
    suffixes.

    Retention is computed as `clock() - retention_seconds` and compared against a
    real, OS-assigned `path.stat().st_mtime` — so, unlike the rotation-suffix tests
    below (which only care about relative ordering/collisions), a retention test
    must start this clock near *real* wall-clock time, not an arbitrary small
    number, or the cutoff math stops meaning anything relative to the files' real
    mtimes.
    """

    def __init__(self, start: float | None = None, step: float = 1.0) -> None:
        self._now = start if start is not None else time.time()
        self._step = step

    def __call__(self) -> float:
        value = self._now
        self._now += self._step
        return value

    def set(self, value: float) -> None:
        self._now = value

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _writer(
    tmp_path: Path,
    *,
    prefix: str = "samples",
    max_file_bytes: int = 1_000_000,
    max_spool_bytes: int = 1_000_000,
    retention_hours: float = 24.0,
    clock: _FakeClock | None = None,
) -> tuple[SpooledRotatingWriter, _FakeClock]:
    fake_clock = clock if clock is not None else _FakeClock()
    writer = SpooledRotatingWriter(
        spool_dir=tmp_path,
        file_prefix=prefix,
        max_file_bytes=max_file_bytes,
        max_spool_bytes=max_spool_bytes,
        retention_hours=retention_hours,
        clock=fake_clock,
    )
    return writer, fake_clock


def test_write_line_creates_spool_dir_lazily(tmp_path: Path) -> None:
    spool_dir = tmp_path / "nested" / "spool"
    assert not spool_dir.exists()
    writer, _ = _writer(spool_dir)

    writer.write_line('{"a":1}')

    assert spool_dir.exists()
    assert writer.active_path.read_text() == '{"a":1}\n'


def test_write_line_appends_to_the_same_active_file(tmp_path: Path) -> None:
    writer, _ = _writer(tmp_path)

    writer.write_line("one")
    writer.write_line("two")

    assert writer.active_path.read_text() == "one\ntwo\n"


def test_rotates_once_active_file_reaches_max_file_bytes(tmp_path: Path) -> None:
    # Each line is 6 bytes on disk ("xxxxx\n"); max_file_bytes=10 means the active
    # file rotates out as soon as it's >= 10 bytes.
    writer, _ = _writer(tmp_path, max_file_bytes=10, max_spool_bytes=1_000_000)

    writer.write_line("xxxxx")  # active file now 6 bytes — under threshold
    writer.write_line("xxxxx")  # active file now 12 bytes — over threshold

    # A third write must rotate the (now-over-threshold) active file out first.
    writer.write_line("zzzzz")

    rotated = sorted(tmp_path.glob("samples-*.jsonl"))
    assert len(rotated) == 1
    assert rotated[0].read_text() == "xxxxx\nxxxxx\n"
    assert writer.active_path.read_text() == "zzzzz\n"


def test_rotation_suffix_collision_falls_back_to_a_counter(tmp_path: Path) -> None:
    clock = _FakeClock(start=42.0, step=0.0)  # every call returns the same instant
    writer, _ = _writer(tmp_path, max_file_bytes=1, clock=clock)

    writer.write_line("a")  # 2 bytes, already >= max_file_bytes=1
    writer.write_line("b")  # must rotate "a" out despite the frozen clock
    writer.write_line("c")  # must rotate "b" out too — same clock value again

    rotated = sorted(tmp_path.glob("samples-*.jsonl"))
    assert len(rotated) == 2
    contents = {path.read_text() for path in rotated}
    assert contents == {"a\n", "b\n"}
    assert writer.active_path.read_text() == "c\n"


def test_retention_hours_deletes_old_rotated_files_independent_of_spool_bytes(
    tmp_path: Path,
) -> None:
    # Starts near *real* wall-clock time (the default) — retention is compared
    # against a real, OS-assigned `st_mtime`, so the fake clock must share that
    # same epoch for the cutoff math to mean anything (see `_FakeClock`'s own
    # docstring).
    clock = _FakeClock(step=0.01)
    writer, _ = _writer(
        tmp_path,
        max_file_bytes=1,
        max_spool_bytes=1_000_000,  # generous — retention alone must do the sweep
        retention_hours=1.0,  # 3600 seconds
        clock=clock,
    )

    writer.write_line("old")  # becomes the active file (already >= max_file_bytes)
    writer.write_line("old2")  # rotates "old" out into its own rotated file
    old_rotated = next(tmp_path.glob("samples-*.jsonl"))
    assert old_rotated.read_text() == "old\n"

    # Jump two real hours ahead — comfortably past the 1-hour retention window —
    # then write again. The sweep that follows every write must now find
    # "old_rotated" stale and delete it, even though `max_spool_bytes` is nowhere
    # near exceeded.
    clock.advance(2 * 3600)
    writer.write_line("new")  # rotates "old2" out too

    assert not old_rotated.exists()


def test_max_spool_bytes_deletes_oldest_rotated_file_first(tmp_path: Path) -> None:
    clock = _FakeClock(start=0.0, step=1.0)
    writer, _ = _writer(
        tmp_path,
        max_file_bytes=1,  # rotate on every write
        max_spool_bytes=8,  # tiny cap — forces eviction
        retention_hours=1_000_000.0,  # effectively disabled for this test
        clock=clock,
    )

    writer.write_line("aaaa")  # rotates to its own file (5 bytes incl. newline)
    writer.write_line("bbbb")  # rotates "aaaa" out as a file; this one now active
    writer.write_line("cccc")  # should push the oldest rotated file out

    rotated = sorted(tmp_path.glob("samples-*.jsonl"))
    contents = {path.read_text() for path in rotated}
    # The very first rotated file ("aaaa") must be gone — oldest-first eviction —
    # while the active file is always spared regardless of the cap.
    assert "aaaa\n" not in contents
    assert writer.active_path.read_text() == "cccc\n"


def test_active_file_is_never_deleted_even_if_it_alone_exceeds_the_cap(
    tmp_path: Path,
) -> None:
    writer, _ = _writer(tmp_path, max_file_bytes=1_000_000, max_spool_bytes=1)

    writer.write_line("this line alone is already far bigger than the tiny cap")

    assert writer.active_path.exists()


def test_two_families_in_the_same_spool_dir_never_interact(tmp_path: Path) -> None:
    samples_writer, _ = _writer(tmp_path, prefix="samples", max_file_bytes=1)
    log_writer, _ = _writer(tmp_path, prefix="log", max_file_bytes=1)

    samples_writer.write_line("sample-1")
    samples_writer.write_line("sample-2")  # rotates sample-1 out
    log_writer.write_line("log-1")
    log_writer.write_line("log-2")  # rotates log-1 out

    sample_files = sorted(tmp_path.glob("samples*.jsonl"))
    log_files = sorted(tmp_path.glob("log*.jsonl"))
    assert len(sample_files) == 2  # one rotated + one active
    assert len(log_files) == 2
    assert samples_writer.active_path.read_text() == "sample-2\n"
    assert log_writer.active_path.read_text() == "log-2\n"
