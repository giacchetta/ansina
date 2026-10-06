from __future__ import annotations

from pathlib import Path

import pytest

from ansina.config import Settings, load_settings
from ansina.heart.eval import publish
from ansina.heart.eval.publish import (
    _collect_bench_items,
    _collect_soak_items,
    _soak_run_id_from_raw_dirname,
    _soak_run_id_from_top_level_name,
)
from ansina.heart.eval.storage import (
    ObjectStoreUnavailableError,
    ObjectStoreUploadError,
)


@pytest.fixture
def loaded_settings(clean_env: None, tmp_cwd: Path) -> Settings:
    return load_settings()


# --- _collect_bench_items -----------------------------------------------------------


def test_collect_bench_items_returns_empty_for_a_missing_directory(
    tmp_path: Path,
) -> None:
    assert _collect_bench_items(tmp_path / "does-not-exist") == []


def test_collect_bench_items_finds_md_and_json_pairs(tmp_path: Path) -> None:
    (tmp_path / "2026-10-06-model-strict.md").write_text("md")
    (tmp_path / "2026-10-06-model-strict.json").write_text("{}")

    items = _collect_bench_items(tmp_path)

    keys = {item.key for item in items}
    assert keys == {
        "kind=bench/dt=2026-10-06/2026-10-06-model-strict.md",
        "kind=bench/dt=2026-10-06/2026-10-06-model-strict.json",
    }


def test_collect_bench_items_ignores_unrelated_extensions(tmp_path: Path) -> None:
    (tmp_path / "2026-10-06-model-strict.txt").write_text("not a report")

    assert _collect_bench_items(tmp_path) == []


def test_collect_bench_items_skips_a_file_with_no_leading_date(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "not-a-date-report.md").write_text("md")

    with caplog.at_level("WARNING"):
        items = _collect_bench_items(tmp_path)

    assert items == []
    assert "no leading ISO date" in caplog.text


# --- soak run-id parsing -------------------------------------------------------------


def test_soak_run_id_from_top_level_name_valid() -> None:
    assert _soak_run_id_from_top_level_name("soak-2026-10-02") == "2026-10-02"
    assert _soak_run_id_from_top_level_name("soak-2026-10-02-2") == "2026-10-02-2"


def test_soak_run_id_from_top_level_name_rejects_wrong_prefix() -> None:
    assert _soak_run_id_from_top_level_name("bench-2026-10-02") is None


def test_soak_run_id_from_top_level_name_rejects_bad_date() -> None:
    assert _soak_run_id_from_top_level_name("soak-not-a-date") is None


def test_soak_run_id_from_raw_dirname_valid() -> None:
    assert _soak_run_id_from_raw_dirname("2026-10-02-raw") == "2026-10-02"


def test_soak_run_id_from_raw_dirname_rejects_wrong_suffix() -> None:
    assert _soak_run_id_from_raw_dirname("2026-10-02-final") is None


def test_soak_run_id_from_raw_dirname_rejects_bad_date() -> None:
    assert _soak_run_id_from_raw_dirname("not-a-date-raw") is None


# --- _collect_soak_items -------------------------------------------------------------


def test_collect_soak_items_returns_empty_for_a_missing_directory(
    tmp_path: Path,
) -> None:
    assert _collect_soak_items(tmp_path / "does-not-exist") == []


def test_collect_soak_items_finds_top_level_and_raw_subdir_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "soak-2026-10-02.md").write_text("md")
    (tmp_path / "soak-2026-10-02.json").write_text("{}")
    raw_dir = tmp_path / "2026-10-02-raw"
    raw_dir.mkdir()
    (raw_dir / "samples.jsonl").write_text("{}\n")
    (raw_dir / "run.log").write_text("log")
    (raw_dir / "journal.json").write_text("{}")
    (raw_dir / "ignored.txt").write_text("not part of the corpus")

    items = _collect_soak_items(tmp_path)

    keys = {item.key for item in items}
    assert keys == {
        "kind=soak/dt=2026-10-02/2026-10-02/soak-2026-10-02.md",
        "kind=soak/dt=2026-10-02/2026-10-02/soak-2026-10-02.json",
        "kind=soak/dt=2026-10-02/2026-10-02/samples.jsonl",
        "kind=soak/dt=2026-10-02/2026-10-02/run.log",
        "kind=soak/dt=2026-10-02/2026-10-02/journal.json",
    }


def test_collect_soak_items_skips_an_unparseable_top_level_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "soak-not-a-date.md").write_text("md")

    assert _collect_soak_items(tmp_path) == []


def test_collect_soak_items_skips_an_unparseable_raw_subdir(tmp_path: Path) -> None:
    bad_dir = tmp_path / "not-a-date-raw"
    bad_dir.mkdir()
    (bad_dir / "samples.jsonl").write_text("{}")

    assert _collect_soak_items(tmp_path) == []


def test_collect_soak_items_ignores_an_unrelated_top_level_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.txt").write_text("not a soak file")

    assert _collect_soak_items(tmp_path) == []


# --- main() ---------------------------------------------------------------------------


def _bench_corpus(bench_dir: Path) -> None:
    bench_dir.mkdir(parents=True)
    (bench_dir / "2026-10-06-model-strict.md").write_text("md")
    (bench_dir / "2026-10-06-model-strict.json").write_text("{}")


def _soak_corpus(soak_dir: Path) -> None:
    soak_dir.mkdir(parents=True)
    (soak_dir / "soak-2026-10-02.md").write_text("md")
    raw_dir = soak_dir / "2026-10-02-raw"
    raw_dir.mkdir()
    (raw_dir / "samples.jsonl").write_text("{}\n")


class _FakeStorage:
    def __init__(
        self,
        *,
        existing: dict[str, frozenset[str]] | None = None,
        fail_upload_keys: frozenset[str] = frozenset(),
        fail_list_prefixes: frozenset[str] = frozenset(),
    ) -> None:
        self._existing = existing or {}
        self._fail_upload_keys = fail_upload_keys
        self._fail_list_prefixes = fail_list_prefixes
        self.uploads: list[tuple[Path, str]] = []

    def existing_keys(self, prefix: str) -> frozenset[str]:
        if prefix in self._fail_list_prefixes:
            raise ObjectStoreUploadError(f"failed to list {prefix}")
        return self._existing.get(prefix, frozenset())

    def upload(self, local_path: Path, key: str) -> None:
        if key in self._fail_upload_keys:
            raise ObjectStoreUploadError(f"failed to upload {key}")
        self.uploads.append((local_path, key))


def test_main_exits_2_on_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise() -> Settings:
        from ansina.config import ConfigError

        raise ConfigError("bad config")

    monkeypatch.setattr(publish, "load_settings", _raise)

    code = publish.main([])

    assert code == 2
    assert "bad config" in capsys.readouterr().err


def test_main_dry_run_prints_every_resolved_key_and_never_builds_storage(
    loaded_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bench_dir = tmp_path / "bench"
    soak_dir = tmp_path / "soak"
    _bench_corpus(bench_dir)
    _soak_corpus(soak_dir)
    monkeypatch.setattr(publish, "load_settings", lambda: loaded_settings)
    monkeypatch.setattr(publish, "configure_logging", lambda _s: None)

    def _fail_if_called(*_a: object, **_k: object) -> object:
        pytest.fail("build_report_storage must never be called under --dry-run")

    monkeypatch.setattr(publish, "build_report_storage", _fail_if_called)

    code = publish.main(
        [
            "--bench-dir",
            str(bench_dir),
            "--soak-dir",
            str(soak_dir),
            "--dry-run",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "DRY RUN: would upload" in out
    assert "2026-10-06-model-strict.md" in out
    assert "soak-2026-10-02.md" in out
    assert "4 file(s) resolved, 0 uploaded" in out


def test_main_exits_2_when_storage_is_misconfigured(
    loaded_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(publish, "load_settings", lambda: loaded_settings)
    monkeypatch.setattr(publish, "configure_logging", lambda _s: None)

    def _raise(_s: object) -> object:
        raise ObjectStoreUnavailableError("telemetry.s3.bucket: not set")

    monkeypatch.setattr(publish, "build_report_storage", _raise)

    code = publish.main(["--bench-dir", str(tmp_path / "bench")])

    assert code == 2
    assert "telemetry.s3.bucket" in capsys.readouterr().err


def test_main_exits_2_when_telemetry_disabled(
    loaded_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(publish, "load_settings", lambda: loaded_settings)
    monkeypatch.setattr(publish, "configure_logging", lambda _s: None)
    monkeypatch.setattr(publish, "build_report_storage", lambda _s: None)

    code = publish.main(["--bench-dir", str(tmp_path / "bench")])

    assert code == 2
    assert "telemetry.s3.enabled is false" in capsys.readouterr().err


def test_main_uploads_everything_not_already_present(
    loaded_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bench_dir = tmp_path / "bench"
    soak_dir = tmp_path / "soak"
    _bench_corpus(bench_dir)
    _soak_corpus(soak_dir)
    storage = _FakeStorage()
    monkeypatch.setattr(publish, "load_settings", lambda: loaded_settings)
    monkeypatch.setattr(publish, "configure_logging", lambda _s: None)
    monkeypatch.setattr(publish, "build_report_storage", lambda _s: storage)

    code = publish.main(["--bench-dir", str(bench_dir), "--soak-dir", str(soak_dir)])

    assert code == 0
    assert len(storage.uploads) == 4  # 2 bench files + 2 soak files
    assert "uploaded 4, skipped 0, 0 failed" in capsys.readouterr().out


def test_main_is_a_verified_no_op_on_a_second_run(
    loaded_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bench_dir = tmp_path / "bench"
    _bench_corpus(bench_dir)
    storage = _FakeStorage(
        existing={
            "kind=bench/": frozenset(
                {
                    "kind=bench/dt=2026-10-06/2026-10-06-model-strict.md",
                    "kind=bench/dt=2026-10-06/2026-10-06-model-strict.json",
                }
            )
        }
    )
    monkeypatch.setattr(publish, "load_settings", lambda: loaded_settings)
    monkeypatch.setattr(publish, "configure_logging", lambda _s: None)
    monkeypatch.setattr(publish, "build_report_storage", lambda _s: storage)

    code = publish.main(["--bench-dir", str(bench_dir)])

    assert code == 0
    assert storage.uploads == []
    assert "uploaded 0, skipped 2, 0 failed" in capsys.readouterr().out


def test_main_counts_an_upload_failure_and_exits_1(
    loaded_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bench_dir = tmp_path / "bench"
    _bench_corpus(bench_dir)
    storage = _FakeStorage(
        fail_upload_keys=frozenset(
            {"kind=bench/dt=2026-10-06/2026-10-06-model-strict.md"}
        )
    )
    monkeypatch.setattr(publish, "load_settings", lambda: loaded_settings)
    monkeypatch.setattr(publish, "configure_logging", lambda _s: None)
    monkeypatch.setattr(publish, "build_report_storage", lambda _s: storage)

    code = publish.main(["--bench-dir", str(bench_dir)])

    assert code == 1
    assert "uploaded 1, skipped 0, 1 failed" in capsys.readouterr().out


def test_main_counts_a_listing_failure_as_every_item_failed(
    loaded_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bench_dir = tmp_path / "bench"
    _bench_corpus(bench_dir)
    storage = _FakeStorage(fail_list_prefixes=frozenset({"kind=bench/"}))
    monkeypatch.setattr(publish, "load_settings", lambda: loaded_settings)
    monkeypatch.setattr(publish, "configure_logging", lambda _s: None)
    monkeypatch.setattr(publish, "build_report_storage", lambda _s: storage)

    code = publish.main(["--bench-dir", str(bench_dir)])

    assert code == 1
    assert storage.uploads == []
    assert "uploaded 0, skipped 0, 2 failed" in capsys.readouterr().out


def test_default_bench_and_soak_dirs_and_dry_run_flag() -> None:
    args = publish._build_parser().parse_args([])

    assert args.bench_dir == Path("docs/heart/bench")
    assert args.soak_dir == Path("docs/heart/soak")
    assert args.dry_run is False
