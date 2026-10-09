from __future__ import annotations

import json
from pathlib import Path

import pytest

from ansina.config import ConfigError, Settings, load_settings
from ansina.heart.eval import __main__ as heart_main
from ansina.heart.eval.provenance import Provenance
from ansina.heart.eval.storage import bench_key
from ansina.heart.runtime import BaseHeartRuntime, HeartUnavailableError


class _FakeRuntime(BaseHeartRuntime):
    """Mirrors `tests/unit/heart/eval/test_runner.py`'s `_FakeHeart`, but starts
    *unloaded* — unlike that one, this test suite needs to assert `main()` itself
    calls `.load()`/`.unload()` around the bench run.
    """

    def __init__(self, *, replies: list[str]) -> None:
        super().__init__(context_tokens=2000, max_output_tokens=50)
        self._replies = replies
        self.load_calls = 0
        self.unload_calls = 0

    def _load_backend(self) -> None:
        self.load_calls += 1

    def _generate(self, prompt: str, max_tokens: int) -> str:
        return self._replies.pop(0)

    def _token_count(self, text: str) -> int:
        return len(text)

    def _unload_backend(self) -> None:
        self.unload_calls += 1


def _fixtures_file(tmp_path: Path) -> Path:
    path = tmp_path / "fixtures.jsonl"
    path.write_text(
        '{"id": "i1", "expect": "idle", "items": [{"source": "s", "text": "t"}]}\n'
        '{"id": "a1", "expect": "act", "items": [{"source": "s", "text": "t"}]}\n'
        '{"id": "e1", "expect": "escalate", '
        '"items": [{"source": "s", "text": "t"}]}\n'
    )
    return path


def _triage_fixtures_file(tmp_path: Path) -> Path:
    path = tmp_path / "triage_fixtures.jsonl"
    path.write_text(
        '{"id": "t1", "expect": "trivial", "request": "what is 2+2?"}\n'
        '{"id": "o1", "expect": "tool-only", "request": "pause the tick loop"}\n'
        '{"id": "c1", "expect": "complex", "request": "design a migration plan"}\n'
    )
    return path


@pytest.fixture
def loaded_settings(clean_env: None, tmp_cwd: Path) -> Settings:
    return load_settings()


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings: Settings,
    runtime: object,
) -> list[Settings]:
    """Wires `load_settings`/`build_heart_runtime`/`configure_logging` the way every
    test below needs, returning the list `build_heart_runtime` was actually called
    with (one entry) so a test can assert on override behavior.
    """
    captured: list[Settings] = []

    def _fake_build_heart_runtime(s: Settings, **_: object) -> object:
        captured.append(s)
        return runtime

    monkeypatch.setattr(heart_main, "load_settings", lambda: settings)
    monkeypatch.setattr(heart_main, "build_heart_runtime", _fake_build_heart_runtime)
    monkeypatch.setattr(heart_main, "configure_logging", lambda _s: None)
    return captured


def test_main_happy_path_writes_reports_and_returns_0_on_gate_pass(
    loaded_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _FakeRuntime(replies=["idle", "act", "escalate"])
    _patch_common(monkeypatch, settings=loaded_settings, runtime=runtime)
    out_dir = tmp_path / "bench"

    code = heart_main.main(
        [
            "--fixtures",
            str(_fixtures_file(tmp_path)),
            "--out-dir",
            str(out_dir),
            "--model-repo",
            "fake/model",
        ]
    )

    assert code == 0
    assert runtime.load_calls == 1
    assert runtime.unload_calls == 1
    md_files = list(out_dir.glob("*.md"))
    json_files = list(out_dir.glob("*.json"))
    assert len(md_files) == 1
    assert len(json_files) == 1
    assert "model" in md_files[0].name
    assert "strict" in md_files[0].name
    payload = json.loads(json_files[0].read_text())
    assert payload["gate"]["passed"] is True
    assert "PASS" in capsys.readouterr().out


def test_main_returns_1_and_prints_fail_when_the_gate_fails(
    loaded_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _FakeRuntime(replies=["banana", "banana", "banana"])
    _patch_common(monkeypatch, settings=loaded_settings, runtime=runtime)
    out_dir = tmp_path / "bench"

    code = heart_main.main(
        ["--fixtures", str(_fixtures_file(tmp_path)), "--out-dir", str(out_dir)]
    )

    assert code == 1
    assert "FAIL" in capsys.readouterr().out


def test_main_exits_1_on_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise() -> Settings:
        raise ConfigError("bad heart config")

    monkeypatch.setattr(heart_main, "load_settings", _raise)

    code = heart_main.main([])

    assert code == 1
    captured = capsys.readouterr()
    assert "bad heart config" in captured.err


def test_main_exits_1_on_fixture_error(
    loaded_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_calls: list[Settings] = []

    def _build_runtime(s: Settings, **_kw: object) -> object:
        runtime_calls.append(s)
        return _FakeRuntime(replies=["idle"])

    monkeypatch.setattr(heart_main, "load_settings", lambda: loaded_settings)
    monkeypatch.setattr(heart_main, "build_heart_runtime", _build_runtime)
    monkeypatch.setattr(heart_main, "configure_logging", lambda _s: None)
    empty_fixtures = tmp_path / "empty.jsonl"
    empty_fixtures.write_text("")

    code = heart_main.main(["--fixtures", str(empty_fixtures)])

    assert code == 1
    assert "no fixtures found" in capsys.readouterr().err
    assert runtime_calls == []


def test_main_exits_1_when_no_viable_heart_runtime(
    loaded_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _raise(_s: Settings, **_kw: object) -> object:
        raise HeartUnavailableError("no viable heart runtime on this host")

    monkeypatch.setattr(heart_main, "load_settings", lambda: loaded_settings)
    monkeypatch.setattr(heart_main, "build_heart_runtime", _raise)
    monkeypatch.setattr(heart_main, "configure_logging", lambda _s: None)
    out_dir = tmp_path / "bench"

    code = heart_main.main(
        ["--fixtures", str(_fixtures_file(tmp_path)), "--out-dir", str(out_dir)]
    )

    assert code == 1
    assert "no viable heart runtime" in capsys.readouterr().err
    assert not out_dir.exists()


def test_main_applies_model_repo_and_max_output_tokens_overrides(
    loaded_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _FakeRuntime(replies=["idle", "act", "escalate"])
    captured = _patch_common(monkeypatch, settings=loaded_settings, runtime=runtime)

    heart_main.main(
        [
            "--fixtures",
            str(_fixtures_file(tmp_path)),
            "--out-dir",
            str(tmp_path / "bench"),
            "--model-repo",
            "custom/repo",
            "--max-output-tokens",
            "77",
        ]
    )

    assert captured[0].heart.model_repo == "custom/repo"
    assert captured[0].heart.max_output_tokens == 77
    assert captured[0] is not loaded_settings


def test_main_passes_settings_through_unchanged_with_no_overrides(
    loaded_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _FakeRuntime(replies=["idle", "act", "escalate"])
    captured = _patch_common(monkeypatch, settings=loaded_settings, runtime=runtime)

    heart_main.main(
        [
            "--fixtures",
            str(_fixtures_file(tmp_path)),
            "--out-dir",
            str(tmp_path / "bench"),
        ]
    )

    assert captured[0] is loaded_settings


def test_main_applies_no_chat_template_override(
    loaded_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _FakeRuntime(replies=["idle", "act", "escalate"])
    captured = _patch_common(monkeypatch, settings=loaded_settings, runtime=runtime)
    out_dir = tmp_path / "bench"

    heart_main.main(
        [
            "--fixtures",
            str(_fixtures_file(tmp_path)),
            "--out-dir",
            str(out_dir),
            "--no-chat-template",
        ]
    )

    assert captured[0].heart.apply_chat_template is False
    assert captured[0] is not loaded_settings
    md_files = list(out_dir.glob("*-notemplate.md"))
    assert len(md_files) == 1


def test_main_writes_resolved_provenance_into_the_report(
    loaded_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _FakeRuntime(replies=["idle", "act", "escalate"])
    _patch_common(monkeypatch, settings=loaded_settings, runtime=runtime)
    monkeypatch.setattr(
        heart_main,
        "resolve_provenance",
        lambda: Provenance(commit="deadbee-dirty", branch="m6-heartbeat"),
    )
    out_dir = tmp_path / "bench"

    heart_main.main(
        ["--fixtures", str(_fixtures_file(tmp_path)), "--out-dir", str(out_dir)]
    )

    json_files = list(out_dir.glob("*.json"))
    payload = json.loads(json_files[0].read_text())
    assert payload["commit"] == "deadbee-dirty"
    assert payload["branch"] == "m6-heartbeat"


def test_default_out_dir_and_suite() -> None:
    args = heart_main._build_parser().parse_args([])

    assert args.out_dir == Path("docs/heart/bench")
    assert args.suite == "tick"
    # Resolved per-suite inside main(), not a fixed default here — see
    # `test_main_resolves_the_default_prompt_variant_per_suite` below.
    assert args.prompt_variant is None
    assert args.model_repo is None
    assert args.fixtures is None
    assert args.max_output_tokens is None
    assert args.no_chat_template is False


def test_prompt_variant_rejects_an_unknown_name() -> None:
    with pytest.raises(SystemExit):
        heart_main._build_parser().parse_args(["--prompt-variant", "nope"])


def test_suite_rejects_an_unknown_name() -> None:
    with pytest.raises(SystemExit):
        heart_main._build_parser().parse_args(["--suite", "nope"])


# --- issue #13: the --suite flag and the triage bench --------------------------------


def test_main_runs_the_triage_suite_when_selected(
    loaded_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _FakeRuntime(replies=["trivial", "tool-only", "complex"])
    _patch_common(monkeypatch, settings=loaded_settings, runtime=runtime)
    out_dir = tmp_path / "bench"

    code = heart_main.main(
        [
            "--suite",
            "triage",
            "--fixtures",
            str(_triage_fixtures_file(tmp_path)),
            "--out-dir",
            str(out_dir),
            "--model-repo",
            "fake/model",
        ]
    )

    assert code == 0
    assert runtime.load_calls == 1
    assert runtime.unload_calls == 1
    md_files = list(out_dir.glob("*.md"))
    assert len(md_files) == 1
    assert "triage" in md_files[0].name
    assert "strict" in md_files[0].name
    payload = json.loads(next(out_dir.glob("*.json")).read_text())
    assert payload["gate"]["passed"] is True
    assert "metrics" in payload and "confusion" in payload["metrics"]
    assert "PASS" in capsys.readouterr().out


def test_main_resolves_the_default_prompt_variant_per_suite(
    loaded_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _FakeRuntime(replies=["trivial", "tool-only", "complex"])
    _patch_common(monkeypatch, settings=loaded_settings, runtime=runtime)
    out_dir = tmp_path / "bench"

    heart_main.main(
        [
            "--suite",
            "triage",
            "--fixtures",
            str(_triage_fixtures_file(tmp_path)),
            "--out-dir",
            str(out_dir),
        ]
    )

    md_files = list(out_dir.glob("*.md"))
    assert len(md_files) == 1
    assert "-triage-strict" in md_files[0].name


def test_main_rejects_a_prompt_variant_not_in_the_selected_suites_map(
    loaded_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`fewshot` is a real tick-suite variant but not a triage one — must be
    rejected at the suite-specific validation step in `main()`, not accepted just
    because `argparse`'s own `--help` union of both suites' names includes it.
    """
    runtime = _FakeRuntime(replies=["trivial"])
    runtime_calls = _patch_common(
        monkeypatch, settings=loaded_settings, runtime=runtime
    )

    code = heart_main.main(
        [
            "--suite",
            "triage",
            "--prompt-variant",
            "fewshot",
            "--fixtures",
            str(_triage_fixtures_file(tmp_path)),
        ]
    )

    assert code == 1
    assert "not a variant of the" in capsys.readouterr().err
    assert runtime_calls == []


# --- issue #59: the upload hook ------------------------------------------------------


class _FakeStorage:
    def __init__(
        self,
        *,
        existing: frozenset[str] = frozenset(),
        raise_on: str | None = None,
    ) -> None:
        self._existing = existing
        self._raise_on = raise_on
        self.uploads: list[tuple[Path, str]] = []

    def existing_keys(self, prefix: str) -> frozenset[str]:
        if self._raise_on == "list":
            raise RuntimeError("list boom")
        return self._existing

    def upload(self, local_path: Path, key: str) -> None:
        if self._raise_on == "upload":
            raise RuntimeError("upload boom")
        self.uploads.append((local_path, key))


def test_upload_hook_is_a_quiet_no_op_when_telemetry_disabled(
    loaded_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`loaded_settings` has `[telemetry.s3] enabled = false` by default — the
    common, unconfigured case — so `build_report_storage` itself returns `None`
    with no patching needed; this just asserts `main()`'s own happy path is
    unaffected by it (already implicitly covered by the happy-path test above,
    made explicit here).
    """
    runtime = _FakeRuntime(replies=["idle", "act", "escalate"])
    _patch_common(monkeypatch, settings=loaded_settings, runtime=runtime)
    out_dir = tmp_path / "bench"

    code = heart_main.main(
        ["--fixtures", str(_fixtures_file(tmp_path)), "--out-dir", str(out_dir)]
    )

    assert code == 0
    assert list(out_dir.glob("*.md")) and list(out_dir.glob("*.json"))


def test_upload_hook_uploads_both_files_when_enabled(
    loaded_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    enabled_settings = loaded_settings.model_copy(
        update={
            "telemetry": loaded_settings.telemetry.model_copy(
                update={
                    "s3": loaded_settings.telemetry.s3.model_copy(
                        update={"enabled": True}
                    )
                }
            )
        }
    )
    runtime = _FakeRuntime(replies=["idle", "act", "escalate"])
    _patch_common(monkeypatch, settings=enabled_settings, runtime=runtime)
    storage = _FakeStorage()
    monkeypatch.setattr(heart_main, "build_report_storage", lambda _s3: storage)
    out_dir = tmp_path / "bench"

    heart_main.main(
        ["--fixtures", str(_fixtures_file(tmp_path)), "--out-dir", str(out_dir)]
    )

    assert len(storage.uploads) == 2
    local_paths = {path for path, _key in storage.uploads}
    keys = {key for _path, key in storage.uploads}
    assert all(path.exists() for path in local_paths)
    md_files = list(out_dir.glob("*.md"))
    assert len(md_files) == 1
    stem = md_files[0].stem
    assert keys == {bench_key(f"{stem}.md"), bench_key(f"{stem}.json")}


def test_upload_hook_suffixes_the_key_on_a_bucket_collision(
    loaded_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    enabled_settings = loaded_settings.model_copy(
        update={
            "telemetry": loaded_settings.telemetry.model_copy(
                update={
                    "s3": loaded_settings.telemetry.s3.model_copy(
                        update={"enabled": True}
                    )
                }
            )
        }
    )
    runtime = _FakeRuntime(replies=["idle", "act", "escalate"])
    _patch_common(monkeypatch, settings=enabled_settings, runtime=runtime)
    out_dir = tmp_path / "bench"
    first_storage = _FakeStorage()
    monkeypatch.setattr(heart_main, "build_report_storage", lambda _s3: first_storage)

    heart_main.main(
        ["--fixtures", str(_fixtures_file(tmp_path)), "--out-dir", str(out_dir)]
    )
    md_files = list(out_dir.glob("*.md"))
    stem = md_files[0].stem
    first_run_keys = {key for _path, key in first_storage.uploads}
    assert first_run_keys == {bench_key(f"{stem}.md"), bench_key(f"{stem}.json")}

    # Second run against a bucket that already has that stem's keys: the upload
    # must go out under a suffixed key, while the local files stay named `stem`
    # (the real write_text calls happen again, overwriting the same local path).
    # A fresh runtime, since the first run already exhausted the scripted replies.
    _patch_common(
        monkeypatch,
        settings=enabled_settings,
        runtime=_FakeRuntime(replies=["idle", "act", "escalate"]),
    )
    colliding_storage = _FakeStorage(existing=frozenset(first_run_keys))
    monkeypatch.setattr(
        heart_main, "build_report_storage", lambda _s3: colliding_storage
    )

    heart_main.main(
        ["--fixtures", str(_fixtures_file(tmp_path)), "--out-dir", str(out_dir)]
    )

    second_run_keys = {key for _path, key in colliding_storage.uploads}
    assert second_run_keys == {
        bench_key(f"{stem}-2.md"),
        bench_key(f"{stem}-2.json"),
    }
    local_paths = {path.name for path, _key in colliding_storage.uploads}
    assert local_paths == {f"{stem}.md", f"{stem}.json"}


def test_upload_hook_logs_and_swallows_a_listing_failure(
    loaded_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    enabled_settings = loaded_settings.model_copy(
        update={
            "telemetry": loaded_settings.telemetry.model_copy(
                update={
                    "s3": loaded_settings.telemetry.s3.model_copy(
                        update={"enabled": True}
                    )
                }
            )
        }
    )
    runtime = _FakeRuntime(replies=["idle", "act", "escalate"])
    _patch_common(monkeypatch, settings=enabled_settings, runtime=runtime)
    storage = _FakeStorage(raise_on="list")
    monkeypatch.setattr(heart_main, "build_report_storage", lambda _s3: storage)
    out_dir = tmp_path / "bench"

    with caplog.at_level("WARNING"):
        code = heart_main.main(
            ["--fixtures", str(_fixtures_file(tmp_path)), "--out-dir", str(out_dir)]
        )

    assert code == 0  # the gate passed; the upload failure never changes this
    assert storage.uploads == []
    assert list(out_dir.glob("*.md")) and list(out_dir.glob("*.json"))
    assert "report upload failed" in caplog.text


def test_upload_hook_logs_and_swallows_an_upload_failure(
    loaded_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    enabled_settings = loaded_settings.model_copy(
        update={
            "telemetry": loaded_settings.telemetry.model_copy(
                update={
                    "s3": loaded_settings.telemetry.s3.model_copy(
                        update={"enabled": True}
                    )
                }
            )
        }
    )
    runtime = _FakeRuntime(replies=["idle", "act", "escalate"])
    _patch_common(monkeypatch, settings=enabled_settings, runtime=runtime)
    storage = _FakeStorage(raise_on="upload")
    monkeypatch.setattr(heart_main, "build_report_storage", lambda _s3: storage)
    out_dir = tmp_path / "bench"

    code = heart_main.main(
        ["--fixtures", str(_fixtures_file(tmp_path)), "--out-dir", str(out_dir)]
    )

    assert code == 0
    assert list(out_dir.glob("*.md")) and list(out_dir.glob("*.json"))
