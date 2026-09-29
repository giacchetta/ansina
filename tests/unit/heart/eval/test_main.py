from __future__ import annotations

import json
from pathlib import Path

import pytest

from ansina.config import ConfigError, Settings, load_settings
from ansina.heart.eval import __main__ as heart_main
from ansina.heart.runtime import BaseHeartRuntime, HeartUnavailableError


class _FakeRuntime(BaseHeartRuntime):
    """Mirrors `tests/unit/heart/eval/test_runner.py`'s `_FakeHeart`, but starts
    *unloaded* — unlike that one, this test suite needs to assert `main()` itself
    calls `.load()`/`.unload()` around the bench run.
    """

    def __init__(self, *, replies: list[str]) -> None:
        super().__init__(context_tokens=1000, max_output_tokens=50)
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
    assert "baseline" in md_files[0].name
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


def test_default_out_dir_and_prompt_variant() -> None:
    args = heart_main._build_parser().parse_args([])

    assert args.out_dir == Path("docs/heart/bench")
    assert args.prompt_variant == "baseline"
    assert args.model_repo is None
    assert args.fixtures is None
    assert args.max_output_tokens is None
    assert args.no_chat_template is False


def test_prompt_variant_rejects_an_unknown_name() -> None:
    with pytest.raises(SystemExit):
        heart_main._build_parser().parse_args(["--prompt-variant", "nope"])
