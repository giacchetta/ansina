from __future__ import annotations

from pathlib import Path

from ansina.config.settings import OpencodeSettings, Settings, TelemetrySettings
from ansina.opencode.workspace import ensure_workspace, isolated_home


def _settings(
    tmp_path: Path, *, enabled: bool = True, isolate_home: bool = True
) -> Settings:
    """Both `telemetry`/`opencode` are passed as complete, already-constructed
    model instances — the same `Settings(telemetry=..., dev=...)` shape
    `tests/unit/dev/test_vector.py` already establishes for this category of
    module. Passing a fully-built nested model (not a plain dict) insulates it
    from this host's own real `ansina.toml`/`ANSINA_*` env vars entirely, with
    no need for the `clean_env`/`tmp_cwd` fixtures `tests/unit/config/
    test_settings.py` uses to test `load_settings()`'s layering itself — a
    different concern this module never exercises.
    """
    opencode = OpencodeSettings(
        enabled=enabled,
        workspace_dir=tmp_path / "workspace",
        isolate_home=isolate_home,
    )
    return Settings(telemetry=TelemetrySettings(enabled=True), opencode=opencode)


def test_ensure_workspace_returns_none_and_creates_nothing_when_disabled(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, enabled=False)

    result = ensure_workspace(settings)

    assert result is None
    assert not (tmp_path / "workspace").exists()


def test_ensure_workspace_creates_the_workspace_and_isolated_home(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, enabled=True, isolate_home=True)

    result = ensure_workspace(settings)

    assert result == (tmp_path / "workspace").resolve()
    assert result.is_dir()
    assert isolated_home(settings).is_dir()
    assert isolated_home(settings) == (tmp_path / "workspace").resolve() / ".home"


def test_ensure_workspace_skips_isolated_home_when_disabled(tmp_path: Path) -> None:
    settings = _settings(tmp_path, enabled=True, isolate_home=False)

    result = ensure_workspace(settings)

    assert result is not None
    assert result.is_dir()
    assert not isolated_home(settings).exists()


def test_ensure_workspace_is_idempotent(tmp_path: Path) -> None:
    settings = _settings(tmp_path, enabled=True, isolate_home=True)

    first = ensure_workspace(settings)
    second = ensure_workspace(settings)

    assert first == second
    assert first is not None
    assert first.is_dir()
