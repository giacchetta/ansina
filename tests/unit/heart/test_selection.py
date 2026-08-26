from __future__ import annotations

from pathlib import Path

import pytest

from ansina.config import Settings, load_settings
from ansina.heart.adapters.mlx import MlxHeartRuntime
from ansina.heart.models import ResolvedModel
from ansina.heart.runtime import HeartUnavailableError
from ansina.heart.selection import build_heart_runtime


@pytest.fixture
def enabled_settings(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> Settings:
    monkeypatch.setenv("ANSINA_HEART__ENABLED", "true")
    return load_settings()


def _resolver(_settings: object) -> ResolvedModel:
    return ResolvedModel(path=Path("/fake/model"), source="path")


def _mock_darwin_arm64_with_mlx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ansina.heart.selection.sys.platform", "darwin")
    monkeypatch.setattr("ansina.heart.selection.platform.machine", lambda: "arm64")
    monkeypatch.setattr(
        "ansina.heart.selection.importlib.util.find_spec",
        lambda name: object(),  # any non-None sentinel — only `is None` is checked
    )


def test_viable_host_builds_mlx_runtime(
    monkeypatch: pytest.MonkeyPatch, enabled_settings: Settings
) -> None:
    _mock_darwin_arm64_with_mlx(monkeypatch)

    runtime = build_heart_runtime(enabled_settings, resolver=_resolver)

    assert isinstance(runtime, MlxHeartRuntime)


def test_non_darwin_platform_is_not_viable(
    monkeypatch: pytest.MonkeyPatch, enabled_settings: Settings
) -> None:
    monkeypatch.setattr("ansina.heart.selection.sys.platform", "linux")

    with pytest.raises(HeartUnavailableError, match="darwin"):
        build_heart_runtime(enabled_settings)


def test_non_arm64_machine_is_not_viable(
    monkeypatch: pytest.MonkeyPatch, enabled_settings: Settings
) -> None:
    monkeypatch.setattr("ansina.heart.selection.sys.platform", "darwin")
    monkeypatch.setattr("ansina.heart.selection.platform.machine", lambda: "x86_64")

    with pytest.raises(HeartUnavailableError, match="arm64"):
        build_heart_runtime(enabled_settings)


def test_mlx_lm_not_importable_is_not_viable(
    monkeypatch: pytest.MonkeyPatch, enabled_settings: Settings
) -> None:
    """Drives the `mlx_lm`-missing branch on an otherwise-viable (darwin/arm64) host."""
    monkeypatch.setattr("ansina.heart.selection.sys.platform", "darwin")
    monkeypatch.setattr("ansina.heart.selection.platform.machine", lambda: "arm64")
    monkeypatch.setattr(
        "ansina.heart.selection.importlib.util.find_spec", lambda name: None
    )

    with pytest.raises(HeartUnavailableError, match="mlx_lm is not installed"):
        build_heart_runtime(enabled_settings)


def test_mlx_importable_on_linux_is_still_not_viable(
    monkeypatch: pytest.MonkeyPatch, enabled_settings: Settings
) -> None:
    """The exact regression this probe exists to prevent: `mlx`'s Linux wheels make
    `import mlx_lm` succeed on a non-Apple-Silicon host, but the platform/machine
    check must still refuse it rather than silently treating it as viable.
    """
    monkeypatch.setattr("ansina.heart.selection.sys.platform", "linux")
    monkeypatch.setattr(
        "ansina.heart.selection.importlib.util.find_spec",
        lambda name: object(),  # any non-None sentinel — only `is None` is checked
    )

    with pytest.raises(HeartUnavailableError):
        build_heart_runtime(enabled_settings)


def test_explicit_mlx_runtime_on_nonviable_host_refuses_rather_than_falls_back(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_HEART__ENABLED", "true")
    monkeypatch.setenv("ANSINA_HEART__RUNTIME", "mlx")
    settings = load_settings()
    monkeypatch.setattr("ansina.heart.selection.sys.platform", "linux")

    with pytest.raises(HeartUnavailableError, match="explicitly 'mlx'"):
        build_heart_runtime(settings)


def test_unavailable_error_names_the_follow_up(
    monkeypatch: pytest.MonkeyPatch, enabled_settings: Settings
) -> None:
    monkeypatch.setattr("ansina.heart.selection.sys.platform", "linux")

    with pytest.raises(HeartUnavailableError, match="follow-up issue"):
        build_heart_runtime(enabled_settings)


def test_resolved_model_is_passed_to_the_mlx_adapter(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_HEART__ENABLED", "true")
    monkeypatch.setenv("ANSINA_HEART__CONTEXT_TOKENS", "4096")
    monkeypatch.setenv("ANSINA_HEART__MAX_OUTPUT_TOKENS", "256")
    settings = load_settings()
    _mock_darwin_arm64_with_mlx(monkeypatch)

    runtime = build_heart_runtime(settings, resolver=_resolver)

    assert isinstance(runtime, MlxHeartRuntime)
    assert runtime.context_tokens == 4096
