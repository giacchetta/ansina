"""Renders OpenCode's own `opencode.json` from Ansina's typed `[opencode]`
settings. See issue #74.

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
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ansina.config.settings import Settings

_SCHEMA_URL = "https://opencode.ai/config.json"
_CONFIG_FILENAME = "opencode.json"


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
    return {"$schema": _SCHEMA_URL, "permission": rendered_permission}


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
