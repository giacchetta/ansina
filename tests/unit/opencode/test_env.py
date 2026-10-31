from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from ansina.config.settings import OpencodeSettings, Settings, TelemetrySettings
from ansina.opencode.config import render_opencode_config
from ansina.opencode.env import (
    ENV_HOME,
    ENV_OPENCODE_CONFIG_CONTENT,
    build_opencode_env,
)
from ansina.opencode.workspace import isolated_home


def _settings(
    tmp_path: Path,
    *,
    isolate_home: bool = True,
    provider_api_key_env: str = "",
    provider_api_key: str | None = None,
    inherit_env_names: tuple[str, ...] = (),
) -> Settings:
    """See `tests/unit/opencode/test_workspace.py`'s own `_settings` docstring
    for why `telemetry`/`opencode` are passed as complete model instances.
    """
    opencode = OpencodeSettings(
        enabled=True,
        workspace_dir=tmp_path / "workspace",
        isolate_home=isolate_home,
        provider_api_key_env=provider_api_key_env,
        provider_api_key=SecretStr(provider_api_key) if provider_api_key else None,
        inherit_env_names=inherit_env_names,
    )
    return Settings(telemetry=TelemetrySettings(enabled=True), opencode=opencode)


def test_build_opencode_env_includes_only_the_allow_listed_names(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    base_env = {
        "PATH": "/usr/bin",
        "TMPDIR": "/tmp",
        "HOME": "/home/someone-else",
        "ANSINA_SECURITY__API_TOKEN": "super-secret-token",
        "ANSINA_BRAIN__API_KEY": "brain-secret",
        "AWS_SECRET_ACCESS_KEY": "s3cr3t",
        "UNRELATED": "noise",
    }

    env = build_opencode_env(settings, base_env=base_env)

    assert env["PATH"] == "/usr/bin"
    assert env["TMPDIR"] == "/tmp"
    assert "ANSINA_SECURITY__API_TOKEN" not in env
    assert "ANSINA_BRAIN__API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "UNRELATED" not in env


def test_build_opencode_env_redirects_home_when_isolate_home_is_true(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, isolate_home=True)
    base_env = {"HOME": "/home/someone-else"}

    env = build_opencode_env(settings, base_env=base_env)

    assert env[ENV_HOME] == str(isolated_home(settings))
    assert env[ENV_HOME] != "/home/someone-else"


def test_build_opencode_env_does_not_set_home_when_isolate_home_is_false(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, isolate_home=False)

    env = build_opencode_env(settings, base_env={"HOME": "/home/someone-else"})

    assert ENV_HOME not in env


def test_build_opencode_env_carries_the_rendered_permission_block(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)

    env = build_opencode_env(settings, base_env={})

    assert json.loads(env[ENV_OPENCODE_CONFIG_CONTENT]) == render_opencode_config(
        settings
    )


def test_build_opencode_env_sets_the_provider_api_key_under_its_configured_name(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path, provider_api_key_env="ANTHROPIC_API_KEY", provider_api_key="sk-real"
    )

    env = build_opencode_env(settings, base_env={})

    assert env["ANTHROPIC_API_KEY"] == "sk-real"


def test_build_opencode_env_omits_the_provider_key_when_unset(tmp_path: Path) -> None:
    settings = _settings(tmp_path, provider_api_key_env="ANTHROPIC_API_KEY")

    env = build_opencode_env(settings, base_env={})

    assert "ANTHROPIC_API_KEY" not in env


def test_build_opencode_env_omits_the_provider_key_when_no_env_name_configured(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, provider_api_key_env="", provider_api_key="sk-real")

    env = build_opencode_env(settings, base_env={})

    assert "sk-real" not in env.values()


def test_build_opencode_env_forwards_only_present_inherit_env_names(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, inherit_env_names=("CUSTOM_PROXY", "ABSENT_NAME"))

    env = build_opencode_env(settings, base_env={"CUSTOM_PROXY": "http://proxy.local"})

    assert env["CUSTOM_PROXY"] == "http://proxy.local"
    assert "ABSENT_NAME" not in env


def test_build_opencode_env_defaults_to_the_real_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/custom/path")
    settings = _settings(tmp_path)

    env = build_opencode_env(settings)

    assert env["PATH"] == "/custom/path"
