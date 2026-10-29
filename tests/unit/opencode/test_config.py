from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ansina.config.settings import (
    OpencodePermissionSettings,
    OpencodeSettings,
    Settings,
    TelemetrySettings,
)
from ansina.opencode.config import render_opencode_config, write_opencode_config


def _settings(tmp_path: Path, **permission_overrides: Any) -> Settings:
    """See `tests/unit/opencode/test_workspace.py`'s own `_settings` docstring
    for why `telemetry`/`opencode` are passed as complete model instances
    rather than through `load_settings()`.
    """
    opencode = OpencodeSettings(
        enabled=True,
        workspace_dir=tmp_path / "workspace",
        permission=OpencodePermissionSettings(**permission_overrides),
    )
    return Settings(telemetry=TelemetrySettings(enabled=True), opencode=opencode)


def test_render_includes_schema_and_every_permission_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    rendered = render_opencode_config(settings)

    assert rendered["$schema"] == "https://opencode.ai/config.json"
    permission = rendered["permission"]
    assert list(permission.keys()) == [
        "*",
        "read",
        "edit",
        "bash",
        "webfetch",
        "websearch",
        "external_directory",
    ]
    assert permission["*"] == "deny"
    assert permission["read"] == "allow"
    assert permission["webfetch"] == "deny"
    assert permission["websearch"] == "deny"
    assert permission["external_directory"] == "deny"


def test_render_edit_workspace_mode_confines_to_the_workspace_glob(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, edit="workspace")

    rendered = render_opencode_config(settings)

    workspace_glob = f"{settings.opencode.workspace_dir}/*"
    assert rendered["permission"]["edit"] == {"*": "deny", workspace_glob: "allow"}


def test_render_edit_deny_and_allow_modes_are_plain_strings(tmp_path: Path) -> None:
    deny_settings = _settings(tmp_path, edit="deny")
    allow_settings = _settings(tmp_path, edit="allow")

    assert render_opencode_config(deny_settings)["permission"]["edit"] == "deny"
    assert render_opencode_config(allow_settings)["permission"]["edit"] == "allow"


def test_render_bash_deny_and_allow_modes_are_plain_strings(tmp_path: Path) -> None:
    deny_settings = _settings(tmp_path, bash="deny")
    allow_settings = _settings(tmp_path, bash="allow")

    assert render_opencode_config(deny_settings)["permission"]["bash"] == "deny"
    assert render_opencode_config(allow_settings)["permission"]["bash"] == "allow"


def test_render_bash_allowlist_emits_deny_floor_then_allow_then_deny_last(
    tmp_path: Path,
) -> None:
    """Emission order is the contract: OpenCode's own documented `bash`/`edit`
    glob-map precedence is last-match-wins, so a denylist glob must be emitted
    *after* the allowlist globs it may overlap with.
    """
    settings = _settings(
        tmp_path,
        bash="allowlist",
        bash_allowlist=("git *", "ls*"),
        bash_denylist=("git push*",),
    )

    bash = render_opencode_config(settings)["permission"]["bash"]

    assert list(bash.items()) == [
        ("*", "deny"),
        ("git *", "allow"),
        ("ls*", "allow"),
        ("git push*", "deny"),
    ]


def test_render_contains_no_ask_value_anywhere(tmp_path: Path) -> None:
    """A headless `--auto` run has nobody to answer a prompt — an `"ask"`
    value would hang a real run until its timeout. Checked against the
    serialized JSON text for the quoted string value `"ask"` specifically
    (not the bare substring "ask", which could otherwise false-positive on
    an unrelated path component, e.g. one containing "task" or "basket") — a
    quoted `"ask"` can only ever appear here as a permission *value*, which
    is exactly what must never happen. This also proves `additional` can't
    sneak one in, since the typed settings model itself can't represent
    `"ask"` at all for the named keys.
    """
    settings = _settings(tmp_path, additional={"glob": "allow", "grep": "deny"})

    rendered_json = json.dumps(render_opencode_config(settings))

    assert '"ask"' not in rendered_json


def test_render_additional_keys_are_appended_after_the_named_keys(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, additional={"glob": "allow", "task": "deny"})

    permission = render_opencode_config(settings)["permission"]

    assert list(permission.keys())[-2:] == ["glob", "task"]
    assert permission["glob"] == "allow"
    assert permission["task"] == "deny"


def test_write_opencode_config_round_trips_as_json(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    workspace = settings.opencode.workspace_dir
    workspace.mkdir(parents=True)

    config_path = write_opencode_config(settings, workspace)

    assert config_path == workspace / "opencode.json"
    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert on_disk == render_opencode_config(settings)


# --- _render_provider / issue #14's custom-provider block -------------------------


def test_render_omits_provider_block_when_base_url_is_unset(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    rendered = render_opencode_config(settings)

    assert "provider" not in rendered


def test_render_emits_provider_block_with_base_url_only(tmp_path: Path) -> None:
    opencode = OpencodeSettings(
        enabled=True,
        workspace_dir=tmp_path / "workspace",
        provider_base_url="https://litellm.example.com/v1",
    )
    settings = Settings(telemetry=TelemetrySettings(enabled=True), opencode=opencode)

    rendered = render_opencode_config(settings)

    assert rendered["provider"] == {
        "ansina": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Ansina",
            "options": {"baseURL": "https://litellm.example.com/v1"},
        }
    }


def test_render_emits_provider_api_key_as_env_template_not_a_secret(
    tmp_path: Path,
) -> None:
    """`options.apiKey` must reference the env var *name* via OpenCode's own
    documented `{env:VARNAME}` templating — never the real secret value, which
    this function never even receives (`provider_api_key` isn't read here).
    """
    opencode = OpencodeSettings(
        enabled=True,
        workspace_dir=tmp_path / "workspace",
        provider_base_url="https://litellm.example.com/v1",
        provider_api_key_env="LITELLM_API_KEY",
    )
    settings = Settings(telemetry=TelemetrySettings(enabled=True), opencode=opencode)

    rendered = render_opencode_config(settings)

    assert rendered["provider"]["ansina"]["options"]["apiKey"] == (
        "{env:LITELLM_API_KEY}"
    )
    rendered_json = json.dumps(rendered)
    assert "LITELLM_API_KEY" in rendered_json  # the name
    assert "sk-" not in rendered_json  # never a literal-looking secret


def test_render_omits_api_key_when_provider_api_key_env_is_unset(
    tmp_path: Path,
) -> None:
    opencode = OpencodeSettings(
        enabled=True,
        workspace_dir=tmp_path / "workspace",
        provider_base_url="https://litellm.example.com/v1",
    )
    settings = Settings(telemetry=TelemetrySettings(enabled=True), opencode=opencode)

    rendered = render_opencode_config(settings)

    assert "apiKey" not in rendered["provider"]["ansina"]["options"]


def test_render_provider_block_sits_between_schema_and_permission(
    tmp_path: Path,
) -> None:
    opencode = OpencodeSettings(
        enabled=True,
        workspace_dir=tmp_path / "workspace",
        provider_base_url="https://litellm.example.com/v1",
    )
    settings = Settings(telemetry=TelemetrySettings(enabled=True), opencode=opencode)

    rendered = render_opencode_config(settings)

    assert list(rendered.keys()) == ["$schema", "provider", "permission"]


def test_render_derives_a_models_entry_from_the_ansina_model_setting(
    tmp_path: Path,
) -> None:
    """Found genuinely required by real-hardware verification (see module
    docstring): `opencode run --model ansina/<id>` against a provider with no
    matching `models` entry fails at admission with `provider.no-route`,
    before ever reaching the network.
    """
    opencode = OpencodeSettings(
        enabled=True,
        workspace_dir=tmp_path / "workspace",
        provider_base_url="https://litellm.example.com/v1",
        model="ansina/GLM-5.3-Flash-low",
    )
    settings = Settings(telemetry=TelemetrySettings(enabled=True), opencode=opencode)

    rendered = render_opencode_config(settings)

    assert rendered["provider"]["ansina"]["models"] == {"GLM-5.3-Flash-low": {}}


def test_render_strips_a_variant_suffix_from_the_derived_model_id(
    tmp_path: Path,
) -> None:
    opencode = OpencodeSettings(
        enabled=True,
        workspace_dir=tmp_path / "workspace",
        provider_base_url="https://litellm.example.com/v1",
        model="ansina/some-model#reasoning",
    )
    settings = Settings(telemetry=TelemetrySettings(enabled=True), opencode=opencode)

    rendered = render_opencode_config(settings)

    assert rendered["provider"]["ansina"]["models"] == {"some-model": {}}


def test_render_omits_models_entry_when_model_names_a_different_provider(
    tmp_path: Path,
) -> None:
    opencode = OpencodeSettings(
        enabled=True,
        workspace_dir=tmp_path / "workspace",
        provider_base_url="https://litellm.example.com/v1",
        model="anthropic/claude",
    )
    settings = Settings(telemetry=TelemetrySettings(enabled=True), opencode=opencode)

    rendered = render_opencode_config(settings)

    assert "models" not in rendered["provider"]["ansina"]


def test_render_omits_models_entry_when_model_is_the_bare_prefix(
    tmp_path: Path,
) -> None:
    """`model = "ansina/"` has no model id after the prefix — the same "empty
    means nothing to derive" discipline every other optional field here uses,
    rather than emitting a `models: {"": {}}` entry.
    """
    opencode = OpencodeSettings(
        enabled=True,
        workspace_dir=tmp_path / "workspace",
        provider_base_url="https://litellm.example.com/v1",
        model="ansina/",
    )
    settings = Settings(telemetry=TelemetrySettings(enabled=True), opencode=opencode)

    rendered = render_opencode_config(settings)

    assert "models" not in rendered["provider"]["ansina"]


def test_write_opencode_config_overwrites_a_prior_run(tmp_path: Path) -> None:
    first_settings = _settings(tmp_path, webfetch="deny")
    workspace = first_settings.opencode.workspace_dir
    workspace.mkdir(parents=True)
    write_opencode_config(first_settings, workspace)

    second_settings = _settings(tmp_path, webfetch="allow")
    config_path = write_opencode_config(second_settings, workspace)

    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert on_disk["permission"]["webfetch"] == "allow"
    assert on_disk == render_opencode_config(second_settings)
