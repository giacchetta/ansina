"""Renders OpenCode's own `opencode.json` from Ansina's typed `[opencode]`
settings. See issue #74; the `provider` block below is issue #14's own small
additive change.

Generated on every preflight/spawn — deliberately unlike `deploy/vector.toml`'s
committed, hand-edited, never-daemon-generated file: the permission block must
be operator-tunable from `ansina.toml` with no code change, and a file this
module writes itself can never drift from the typed settings that produced it
(a hand-edited file always can).

Emission order inside `permission` is load-bearing, not cosmetic: OpenCode's
own documented precedence for `bash`/`edit`'s glob-map values is "rules are
evaluated by pattern match, with the last matching rule winning" — so `"*"`
(the deny floor) is always emitted first, then every allowlist glob, then
every denylist glob *last*, so an overlapping denylist entry always beats an
allowlist one rather than the reverse. `write_opencode_config`'s
`sort_keys=False` is deliberate for the identical reason — a sorted
re-ordering would silently change which glob rule wins.

`_render_provider()` (issue #14) is the custom-provider block for OpenCode's
own driving model, when `[opencode] provider_base_url` is set — i.e. when that
model is a self-hosted/custom OpenAI-compatible endpoint (a LiteLLM gateway, in
the first real deployment of this) rather than one of OpenCode's own built-in
providers. Verified against the real opencode.ai docs (`/docs/providers`'s own
"Custom provider" section) rather than assumed: a custom OpenAI-compatible
provider is `provider.<id>.npm = "@ai-sdk/openai-compatible"` plus
`options.baseURL`/`options.apiKey`, and `options.apiKey` supports a documented
`"{env:VARNAME}"` templating form that lets the *value* stay in the child
process's own environment — this function only ever emits the env var *name*
(`[opencode] provider_api_key_env`), never `provider_api_key`'s actual secret
value, so the rendered file is safe to write to disk and to embed verbatim into
`OPENCODE_CONFIG_CONTENT` (`ansina.opencode.env`). `_CUSTOM_PROVIDER_ID` is a
fixed constant, not a setting — Ansina configures at most one custom provider
for OpenCode's own model, so a second knob to name it would add no real
flexibility. Whether OpenCode's own `--model <id>/<model>` resolution also
needs a `models.<model-id>` entry under this block (the docs' own worked
example includes one, but only ever to set a display name/context limits, not
stated as required for a direct CLI invocation) is left to real-hardware
verification — see `docs/opencode.md`'s own "verify, don't assume" discipline;
not added here speculatively.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ansina.config.settings import Settings

_SCHEMA_URL = "https://opencode.ai/config.json"
_CONFIG_FILENAME = "opencode.json"
_CUSTOM_PROVIDER_ID = "ansina"


def _render_glob_permission(
    *, allowlist: tuple[str, ...], denylist: tuple[str, ...]
) -> dict[str, str]:
    """`"*": "deny"` first (the floor), then every allowlist glob, then every
    denylist glob *last* — see module docstring for why this order is the
    contract, not an incidental detail of dict insertion order.
    """
    rendered: dict[str, str] = {"*": "deny"}
    for glob in allowlist:
        rendered[glob] = "allow"
    for glob in denylist:
        rendered[glob] = "deny"
    return rendered


def _render_edit_permission(settings: Settings) -> str | dict[str, str]:
    opencode = settings.opencode
    mode = opencode.permission.edit
    if mode in ("deny", "allow"):
        return mode
    # "workspace": confined to the workspace directory itself, nothing else.
    workspace_glob = f"{opencode.workspace_dir}/*"
    return {"*": "deny", workspace_glob: "allow"}


def _render_bash_permission(settings: Settings) -> str | dict[str, str]:
    permission = settings.opencode.permission
    mode = permission.bash
    if mode in ("deny", "allow"):
        return mode
    # "allowlist": the deny floor, then the configured allow/deny globs.
    return _render_glob_permission(
        allowlist=permission.bash_allowlist, denylist=permission.bash_denylist
    )


def _render_provider(settings: Settings) -> dict[str, Any] | None:
    """The custom-provider block for OpenCode's own driving model, or `None`
    when `[opencode] provider_base_url` is unset — see module docstring for the
    documented shape this mirrors and why `apiKey` never carries a real secret.
    """
    opencode = settings.opencode
    if not opencode.provider_base_url:
        return None
    options: dict[str, str] = {"baseURL": opencode.provider_base_url}
    if opencode.provider_api_key_env:
        options["apiKey"] = f"{{env:{opencode.provider_api_key_env}}}"
    return {
        _CUSTOM_PROVIDER_ID: {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Ansina",
            "options": options,
        }
    }


def render_opencode_config(settings: Settings) -> dict[str, Any]:
    """The pure, deterministic rendering `write_opencode_config` serializes —
    split out so the unit suite can assert its shape (key order, no `"ask"`
    anywhere) without touching a filesystem, and so `ansina.opencode.env` can
    embed the identical rendering into `OPENCODE_CONFIG_CONTENT`.
    """
    permission = settings.opencode.permission
    rendered_permission: dict[str, Any] = {
        "*": permission.default,
        "read": permission.read,
        "edit": _render_edit_permission(settings),
        "bash": _render_bash_permission(settings),
        "webfetch": permission.webfetch,
        "websearch": permission.websearch,
        "external_directory": permission.external_directory,
    }
    # Last, deliberately: an operator-named key here can reach any other
    # OpenCode permission key this model doesn't name explicitly (see
    # `OpencodePermissionSettings.additional`'s own docstring) — appended
    # after, never merged ahead of, the keys this module controls directly.
    rendered_permission.update(permission.additional)

    rendered: dict[str, Any] = {"$schema": _SCHEMA_URL}
    provider = _render_provider(settings)
    if provider is not None:
        rendered["provider"] = provider
    rendered["permission"] = rendered_permission
    return rendered


def write_opencode_config(settings: Settings, workspace: Path) -> Path:
    """Writes the rendered config to `<workspace>/opencode.json`, overwriting
    whatever was already there — this file always reflects the *current*
    typed settings, never a prior run's. `indent=2` for a human reading it
    while debugging; `sort_keys=False` (`json.dumps`'s own default, stated
    explicitly here since it is load-bearing, not incidental) preserves the
    emission order `render_opencode_config` built.
    """
    config_path = workspace / _CONFIG_FILENAME
    config_path.write_text(
        json.dumps(render_opencode_config(settings), indent=2, sort_keys=False),
        encoding="utf-8",
    )
    return config_path
