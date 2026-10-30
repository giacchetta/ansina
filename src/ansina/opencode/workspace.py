"""Confined workspace provisioning for the OpenCode child. See issue #74.

`ensure_workspace`/`isolated_home` are the two functions `ansina.opencode.env`
and `ansina.opencode.preflight` both build on, so the isolated-`HOME` path is
spelled in exactly one place — it can never drift between what `env.py` sets
`HOME` to and what `preflight.py` checks is writable.
"""

from __future__ import annotations

from pathlib import Path

from ansina.config.settings import Settings

_ISOLATED_HOME_DIR_NAME = ".home"


def isolated_home(settings: Settings) -> Path:
    """The directory `ansina.opencode.env.build_opencode_env` redirects the
    child's own `HOME` to, when `[opencode] isolate_home` is `True` — a fixed
    subdirectory of `workspace_dir`, not a separate setting, so it is always
    provisioned (or not) by the exact same `ensure_workspace` call that
    provisions `workspace_dir` itself.
    """
    return settings.opencode.workspace_dir / _ISOLATED_HOME_DIR_NAME


def ensure_workspace(settings: Settings) -> Path | None:
    """`None` when `[opencode] enabled = false` — creates nothing, the same
    "off means off" shape every other Dev-Mode-adjacent factory in this
    codebase uses. Otherwise idempotent (`mkdir(parents=True, exist_ok=True)`,
    the `SpooledRotatingWriter` first-write discipline): provisions
    `workspace_dir` itself, and `isolated_home(settings)` too when
    `isolate_home` is set, returning `workspace_dir`.
    """
    opencode = settings.opencode
    if not opencode.enabled:
        return None
    workspace = opencode.workspace_dir
    workspace.mkdir(parents=True, exist_ok=True)
    if opencode.isolate_home:
        isolated_home(settings).mkdir(parents=True, exist_ok=True)
    return workspace
