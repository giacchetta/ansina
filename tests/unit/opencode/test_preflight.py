from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import ansina.opencode.preflight as preflight_module
from ansina.config.settings import OpencodeSettings, Settings, TelemetrySettings
from ansina.opencode.config import render_opencode_config
from ansina.opencode.preflight import (
    _find_looser_than_configured,
    _scan_for_ask,
    build_opencode_service,
    preflight,
)
from ansina.opencode.runner import OpencodeCliRunner


def _settings(
    tmp_path: Path,
    *,
    enabled: bool = True,
    binary: str = "opencode",
    required_version_prefix: str = "",
    preflight_timeout_seconds: float = 30.0,
) -> Settings:
    """See `tests/unit/opencode/test_workspace.py`'s own `_settings` docstring
    for why `telemetry`/`opencode` are passed as complete model instances.
    """
    opencode = OpencodeSettings(
        enabled=enabled,
        binary=binary,
        required_version_prefix=required_version_prefix,
        workspace_dir=tmp_path / "workspace",
        preflight_timeout_seconds=preflight_timeout_seconds,
    )
    return Settings(telemetry=TelemetrySettings(enabled=True), opencode=opencode)


def _make_runner(
    *,
    version_stdout: str = "1.19.3\n",
    version_returncode: int = 0,
    debug_config_permission: dict[str, Any] | None = None,
    debug_config_returncode: int = 0,
    debug_config_stdout_override: str | None = None,
) -> Any:
    """A single fake `VersionRunner` dispatching on the subcommand
    (`--version` vs `debug config`) — both real preflight checks share one
    `runner` parameter, the same `ValidateRunner`-fake shape
    `tests/unit/dev/test_vector.py` already establishes.
    """

    def _runner(
        args: Any,
        *,
        env: dict[str, str],
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        if args[1:] == ["--version"]:
            return subprocess.CompletedProcess(
                args, returncode=version_returncode, stdout=version_stdout, stderr=""
            )
        assert args[1:] == ["debug", "config"]
        if debug_config_stdout_override is not None:
            stdout = debug_config_stdout_override
        else:
            permission = (
                debug_config_permission if debug_config_permission is not None else {}
            )
            stdout = json.dumps({"permission": permission})
        return subprocess.CompletedProcess(
            args, returncode=debug_config_returncode, stdout=stdout, stderr=""
        )

    return _runner


# --- preflight: enabled / telemetry gates ---------------------------------------


def test_preflight_skips_when_disabled(tmp_path: Path) -> None:
    settings = _settings(tmp_path, enabled=False)

    result = preflight(settings)

    assert result.opencode_path is None
    assert result.skip_reason is not None
    assert "opencode.enabled is false" in result.skip_reason


def test_preflight_skips_when_telemetry_disabled(tmp_path: Path) -> None:
    """Unreachable through any real `Settings` tree — `Settings.
    _refuse_unobserved_executor` refuses to construct one with `opencode.
    enabled=True` and `telemetry.enabled=False` in the first place. Forced
    here via `model_copy(update=...)`, which (unlike the constructor)
    bypasses model validators entirely — exactly the "future caller outside
    `Settings`'s own validation" scenario this defense-in-depth check guards
    against, named explicitly in this module's own docstring.
    """
    settings = _settings(tmp_path, enabled=True).model_copy(
        update={"telemetry": TelemetrySettings(enabled=False)}
    )
    assert settings.telemetry.enabled is False
    assert settings.opencode.enabled is True

    result = preflight(settings)

    assert result.opencode_path is None
    assert result.skip_reason is not None
    assert "telemetry.enabled is false" in result.skip_reason


# --- preflight: binary resolution -----------------------------------------------


def test_preflight_skips_when_binary_not_on_path(tmp_path: Path) -> None:
    settings = _settings(tmp_path, binary="definitely-not-a-real-binary-xyz")

    result = preflight(settings)

    assert result.opencode_path is None
    assert result.skip_reason is not None
    assert "not found on PATH" in result.skip_reason


# --- preflight: version check ----------------------------------------------------


def test_preflight_skips_the_version_check_when_prefix_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`required_version_prefix = ""` must never call `opencode --version` at
    all — a runner that would raise if ever invoked with that subcommand
    proves it.
    """
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/opencode")

    def _runner(args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[1:] == ["--version"]:
            raise AssertionError("version check should have been skipped")
        return subprocess.CompletedProcess(
            args, returncode=0, stdout=json.dumps({"permission": {}}), stderr=""
        )

    settings = _settings(tmp_path, required_version_prefix="")

    result = preflight(settings, runner=_runner)

    assert result.skip_reason is None


def test_preflight_skips_when_version_check_fails_to_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/opencode")

    def _runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("opencode: command not found")

    settings = _settings(tmp_path, required_version_prefix="1.")

    result = preflight(settings, runner=_runner)

    assert result.opencode_path is None
    assert result.skip_reason is not None
    assert "failed to run" in result.skip_reason


def test_preflight_skips_when_version_check_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/opencode")

    def _runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="opencode", timeout=30.0)

    settings = _settings(tmp_path, required_version_prefix="1.")

    result = preflight(settings, runner=_runner)

    assert result.opencode_path is None
    assert result.skip_reason is not None
    assert "failed to run" in result.skip_reason


def test_preflight_skips_when_version_prefix_does_not_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/opencode")
    runner = _make_runner(version_stdout="0.9.0\n")
    settings = _settings(tmp_path, required_version_prefix="1.")

    result = preflight(settings, runner=runner)

    assert result.opencode_path is None
    assert result.skip_reason is not None
    assert "does not start with" in result.skip_reason


def test_preflight_skips_when_version_check_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/opencode")
    runner = _make_runner(version_stdout="1.19.3\n", version_returncode=1)
    settings = _settings(tmp_path, required_version_prefix="1.")

    result = preflight(settings, runner=runner)

    assert result.opencode_path is None
    assert result.skip_reason is not None
    assert "does not start with" in result.skip_reason


def test_preflight_passes_the_version_check_with_a_matching_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/opencode")
    runner = _make_runner(version_stdout="1.19.3\n")
    settings = _settings(tmp_path, required_version_prefix="1.")

    result = preflight(settings, runner=runner)

    assert result.skip_reason is None


# --- preflight: workspace provisioning ------------------------------------------


def test_preflight_skips_when_the_workspace_cannot_be_provisioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreachable via the real `ensure_workspace` once `opencode.enabled` has
    already passed (it returns `None` only when disabled) — forced here by
    monkeypatching it directly, proving `preflight` doesn't blindly trust its
    own helper.
    """
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/opencode")
    monkeypatch.setattr(preflight_module, "ensure_workspace", lambda _settings: None)
    settings = _settings(tmp_path)

    result = preflight(settings)

    assert result.opencode_path is None
    assert result.skip_reason is not None
    assert "could not be provisioned" in result.skip_reason


def test_preflight_skips_when_the_workspace_path_is_not_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/opencode")
    not_a_dir = tmp_path / "not-a-directory"
    not_a_dir.write_text("I am a file, not a directory")
    monkeypatch.setattr(
        preflight_module, "ensure_workspace", lambda _settings: not_a_dir
    )
    settings = _settings(tmp_path)

    result = preflight(settings)

    assert result.opencode_path is None
    assert result.skip_reason is not None
    assert "could not be provisioned" in result.skip_reason


# --- preflight: generated config round-trip -------------------------------------


def test_preflight_skips_when_the_generated_config_does_not_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreachable via any real `[opencode]` settings tree — `render_
    opencode_config` only ever builds plain, JSON-serializable values from a
    typed Pydantic model. Forced here by monkeypatching it to return a
    non-serializable value, the same "defensive, never-trust-a-helper" shape
    `test_preflight_skips_when_the_workspace_path_is_not_a_directory` already
    uses above.
    """
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/opencode")
    monkeypatch.setattr(
        preflight_module, "render_opencode_config", lambda _settings: {"bad": object()}
    )
    settings = _settings(tmp_path)

    result = preflight(settings)

    assert result.opencode_path is None
    assert result.skip_reason is not None
    assert "did not round-trip as JSON" in result.skip_reason


# --- preflight: effective-permission assertion ----------------------------------


def test_preflight_passes_when_debug_config_fails_to_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Best-effort: a failure to even run `opencode debug config` must never
    fail the whole preflight — it just skips this one extra check.
    """
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/opencode")

    def _runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("debug config unsupported on this opencode version")

    settings = _settings(tmp_path)

    result = preflight(settings, runner=_runner)

    assert result.opencode_path == "/usr/opencode"
    assert result.skip_reason is None


def test_preflight_passes_when_debug_config_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/opencode")
    runner = _make_runner(debug_config_returncode=1)
    settings = _settings(tmp_path)

    result = preflight(settings, runner=runner)

    assert result.skip_reason is None


def test_preflight_passes_when_debug_config_output_is_not_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/opencode")
    runner = _make_runner(debug_config_stdout_override="not json at all")
    settings = _settings(tmp_path)

    result = preflight(settings, runner=runner)

    assert result.skip_reason is None


def test_preflight_passes_when_debug_config_has_no_permission_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/opencode")
    runner = _make_runner(debug_config_stdout_override=json.dumps({"other": True}))
    settings = _settings(tmp_path)

    result = preflight(settings, runner=runner)

    assert result.skip_reason is None


def test_preflight_detects_an_ask_value_anywhere_in_the_effective_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/opencode")
    runner = _make_runner(
        debug_config_permission={"bash": {"git *": "ask"}, "*": "deny"}
    )
    settings = _settings(tmp_path)

    result = preflight(settings, runner=runner)

    assert result.opencode_path is None
    assert result.skip_reason is not None
    assert "looser than configured" in result.skip_reason
    assert "bash.git *" in result.skip_reason


def test_preflight_detects_a_scalar_value_looser_than_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/opencode")
    runner = _make_runner(debug_config_permission={"webfetch": "allow"})
    settings = _settings(tmp_path)

    result = preflight(settings, runner=runner)

    assert result.opencode_path is None
    assert result.skip_reason is not None
    assert "looser than configured" in result.skip_reason
    assert "webfetch" in result.skip_reason


def test_preflight_passes_every_check_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/opencode")
    settings = _settings(tmp_path, required_version_prefix="1.")
    configured_permission = render_opencode_config(settings)["permission"]
    captured: dict[str, Any] = {}

    def _runner(
        args: Any,
        *,
        env: dict[str, str],
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        captured.setdefault("calls", []).append(args)
        captured["env"] = env
        if args[1:] == ["--version"]:
            return subprocess.CompletedProcess(
                args, returncode=0, stdout="1.19.3\n", stderr=""
            )
        return subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout=json.dumps({"permission": configured_permission}),
            stderr="",
        )

    result = preflight(settings, runner=_runner)

    assert result.opencode_path == "/usr/opencode"
    assert result.skip_reason is None
    assert captured["calls"] == [
        ["/usr/opencode", "--version"],
        ["/usr/opencode", "debug", "config"],
    ]
    assert "OPENCODE_CONFIG_CONTENT" in captured["env"]
    embedded = json.loads(captured["env"]["OPENCODE_CONFIG_CONTENT"])
    assert embedded["permission"] == configured_permission
    assert not (settings.opencode.workspace_dir / "opencode.json").exists()


# --- _scan_for_ask / _find_looser_than_configured (pure helpers) ----------------


def test_scan_for_ask_finds_a_nested_ask_value() -> None:
    structure = {"bash": {"*": "deny", "git *": "ask"}, "read": "allow"}

    found = _scan_for_ask(structure, "")

    assert found == ["bash.git *"]


def test_scan_for_ask_returns_empty_for_a_clean_structure() -> None:
    structure = {"bash": {"*": "deny", "git *": "allow"}, "read": "allow"}

    assert _scan_for_ask(structure, "") == []


def test_scan_for_ask_ignores_a_non_string_non_dict_leaf_value() -> None:
    """A real `opencode debug config` dump could carry a non-permission field
    of any JSON type (bool/int/list/null) nested alongside the permission
    block — `_scan_for_ask` must skip it rather than raise.
    """
    assert _scan_for_ask(True, "doom_loop") == []
    assert _scan_for_ask(3, "some_count") == []
    assert _scan_for_ask(None, "unset") == []


def test_find_looser_than_configured_ignores_a_tightened_scalar(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)

    offending = _find_looser_than_configured(settings, {"webfetch": "deny"})

    assert offending == []


def test_find_looser_than_configured_ignores_a_non_string_scalar(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)

    offending = _find_looser_than_configured(settings, {"webfetch": {"*": "deny"}})

    assert offending == []


# --- build_opencode_service -----------------------------------------------------


def test_build_opencode_service_returns_none_on_a_failed_preflight(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, enabled=False)

    assert build_opencode_service(settings) is None


def test_build_opencode_service_returns_a_runner_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #75: `build_opencode_service` now returns a real
    `OpencodeCliRunner`, already constructed against the preflight-resolved
    binary, rather than the bare `OpencodePreflightResult` sentinel issue
    #74 shipped — the `None`-when-off contract itself is unchanged (see the
    failed-preflight test above).
    """
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/opencode")
    settings = _settings(tmp_path)

    service = build_opencode_service(settings, runner=_make_runner())

    assert isinstance(service, OpencodeCliRunner)
    assert service.opencode_path == "/usr/opencode"
