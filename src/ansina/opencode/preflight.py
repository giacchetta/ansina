"""Preflight checks for the OpenCode child, and `build_opencode_service` — the
`None`-when-off factory that is the real `ansina.opencode` entry point. See
issue #74 (the checks themselves) and issue #75 (the runner they gate).

No executor is spawned by `preflight()` itself, in this issue or the next —
it only ever confirms the child *could* run (`opencode --version`, `opencode
debug config`); `OpencodeCliRunner.run()` (`ansina.opencode.runner`, issue
#75) is the first thing that actually spawns a real `opencode` process to do
work, and `build_opencode_service` below is what hands a caller one, already
wired to the preflight-resolved `opencode_path` — a failed preflight still
means `None`, the same shape it always has. `preflight()` itself does call
out to the real binary twice — the first module in this codebase to shell out
from outside `ansina.dev`'s boot-time exception or `heart.eval`'s dev-tooling
boundary; see `ansina.opencode`'s own module docstring for the further-
widened invariant issue #75 introduces for the actual runner.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ansina.config.settings import Settings
from ansina.logging import get_logger
from ansina.opencode.config import render_opencode_config, write_opencode_config
from ansina.opencode.env import build_opencode_env
from ansina.opencode.runner import OpencodeCliRunner
from ansina.opencode.workspace import ensure_workspace

logger = get_logger(__name__)


class VersionRunner(Protocol):
    """The exact slice of `subprocess.run` this module calls — mirrors
    `ansina.dev.vector.ValidateRunner`'s own narrow-slice-of-the-stdlib shape,
    so the unit suite injects a fake with no real `opencode` binary.
    """

    def __call__(
        self,
        args: Sequence[str],
        *,
        env: dict[str, str],
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True, slots=True)
class OpencodePreflightResult:
    """Exactly one of the two fields is set, never both, never neither:
    `opencode_path` on a clean pass through every check, `skip_reason` the
    moment any one check fails — mirrors `ansina.dev.vector.PreflightResult`.
    """

    opencode_path: str | None
    skip_reason: str | None


def _skip(reason: str) -> OpencodePreflightResult:
    logger.warning("opencode: not started", extra={"reason": reason})
    return OpencodePreflightResult(opencode_path=None, skip_reason=reason)


_SCALAR_PERMISSION_KEYS = ("*", "read", "webfetch", "websearch", "external_directory")


def _scan_for_ask(value: Any, path: str) -> list[str]:  # noqa: ANN401
    """Recursively walks a resolved permission structure (scalars, and the
    glob-map objects `edit`/`bash` may resolve to) looking for a literal
    `"ask"` anywhere — a headless `--auto` run has nobody to answer a prompt,
    so even one surviving `"ask"` (e.g. from a global config layer this
    module's own `HOME` redirect didn't reach) would hang a real run until
    its timeout.
    """
    if isinstance(value, str):
        return [path] if value == "ask" else []
    if isinstance(value, dict):
        found: list[str] = []
        for key, nested in value.items():
            found.extend(_scan_for_ask(nested, f"{path}.{key}" if path else str(key)))
        return found
    return []


def _find_looser_than_configured(
    settings: Settings, effective: dict[str, Any]
) -> list[str]:
    """Two checks over the *effective*, fully-resolved permission block
    `opencode debug config` reports, versus what this module itself rendered:
    (1) no `"ask"` anywhere, at any depth (`_scan_for_ask`); (2) for the plain
    scalar keys this module configures directly, the resolved value is never
    `"allow"` where Ansina configured `"deny"`. `edit`/`bash` are deliberately
    not compared value-for-value beyond the `"ask"` scan — `render_
    opencode_config`'s own emission-order unit tests already prove what this
    module *asked for*; a generic glob-precedence diff against whatever
    `opencode debug config` normalizes its own output to is out of scope for
    a preflight check that must stay fast, local, and never itself a new
    source of false failures.
    """
    configured = render_opencode_config(settings)["permission"]
    offending = _scan_for_ask(effective, "")
    for key in _SCALAR_PERMISSION_KEYS:
        resolved_value = effective.get(key)
        configured_value = configured.get(key)
        if (
            isinstance(resolved_value, str)
            and configured_value == "deny"
            and resolved_value == "allow"
        ):
            offending.append(f"{key}={resolved_value!r} (configured deny)")
    return offending


def _resolve_effective_permission(
    settings: Settings, opencode_path: str, *, runner: VersionRunner
) -> dict[str, Any] | None:
    """Best-effort: `opencode debug config` prints OpenCode's own fully
    merged configuration (every source — remote, global, `OPENCODE_CONFIG`,
    project, `OPENCODE_CONFIG_CONTENT`, managed, macOS managed preferences —
    combined in its own documented precedence order). Returns its
    `permission` block, or `None` on any failure to run or parse it — this
    check exists to catch an *extra* source of looseness beyond what this
    module already controls, not to become a second point of failure for an
    otherwise-healthy preflight; a version of `opencode` with no `debug
    config` subcommand simply skips this one check.
    """
    try:
        completed = runner(
            [opencode_path, "debug", "config"],
            env=build_opencode_env(settings),
            capture_output=True,
            text=True,
            timeout=settings.opencode.preflight_timeout_seconds,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    if completed.returncode != 0:
        return None
    try:
        resolved = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    permission = resolved.get("permission") if isinstance(resolved, dict) else None
    return permission if isinstance(permission, dict) else None


def preflight(
    settings: Settings, *, runner: VersionRunner = subprocess.run
) -> OpencodePreflightResult:
    """Logged-skip checks, cheapest first, each with its own specific log
    line — "a shipper with nothing to ship is worse than no shipper"
    (`ansina.dev.vector.preflight`'s own reasoning) extends here too: an
    executor Ansina can't observe is worse than no executor at all.

    That reasoning is also enforced at boot, unconditionally, by
    `Settings._refuse_unobserved_executor` — so the `telemetry.enabled` check
    below is unreachable through any real `Settings` tree. It is kept anyway,
    as explicit defense in depth (named in this issue's own AC), in case a
    future caller ever constructs `OpencodeSettings` outside `Settings`'s own
    validation.
    """
    opencode = settings.opencode
    if not opencode.enabled:
        return _skip("opencode.enabled is false — nothing to preflight")

    if not settings.telemetry.enabled:
        return _skip("telemetry.enabled is false — an unobserved executor is refused")

    opencode_path = shutil.which(opencode.binary)
    if opencode_path is None:
        return _skip(f"opencode binary {opencode.binary!r} not found on PATH")

    if opencode.required_version_prefix:
        try:
            completed = runner(
                [opencode_path, "--version"],
                env=build_opencode_env(settings),
                capture_output=True,
                text=True,
                timeout=opencode.preflight_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _skip(f"opencode --version failed to run: {exc}")
        # A raw-prefix match against the trimmed output, not a semver
        # comparison, and deliberately not assuming one output shape: OpenCode
        # 1.x prints a bare `"1.19.3"`; 2.x prints `"opencode v2.1.1"` (binary
        # name + a leading `v`) — `OpencodeSettings.required_version_prefix`'s
        # own docstring says so, so the configured prefix is the operator's
        # responsibility to match against whatever their pinned release
        # actually emits, the same way `[dev].vector_binary`'s 0.45.x pin is.
        version = completed.stdout.strip() or completed.stderr.strip()
        if completed.returncode != 0 or not version.startswith(
            opencode.required_version_prefix
        ):
            return _skip(
                f"opencode --version reported {version!r}, which does not "
                f"start with the required {opencode.required_version_prefix!r}"
            )

    workspace = ensure_workspace(settings)
    if workspace is None or not workspace.is_dir():
        # Unreachable in practice via the real `ensure_workspace` once the
        # `opencode.enabled` check above has already passed (it returns
        # `None` only when disabled) — kept as a defensive, never-silently-
        # trust-a-helper check rather than an assumption; the unit suite
        # forces this branch via a monkeypatched `ensure_workspace`.
        return _skip(f"workspace {opencode.workspace_dir} could not be provisioned")

    config_path = write_opencode_config(settings, workspace)
    try:
        json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _skip(f"generated {config_path} did not round-trip as JSON: {exc}")

    effective = _resolve_effective_permission(settings, opencode_path, runner=runner)
    if effective is not None:
        offending = _find_looser_than_configured(settings, effective)
        if offending:
            return _skip(
                "effective opencode permissions are looser than configured "
                f"({', '.join(offending)}) — a global or managed OpenCode "
                "config on this host may be merging in above ours"
            )

    return OpencodePreflightResult(opencode_path=opencode_path, skip_reason=None)


def build_opencode_service(
    settings: Settings, *, runner: VersionRunner = subprocess.run
) -> OpencodeCliRunner | None:
    """`None` on any failed preflight check — the `build_vector_sidecar`/
    `build_heart_runtime`/`build_brain_provider`/`build_report_storage`
    no-op shape. Returns a real `OpencodeCliRunner`, already constructed
    with the preflight-resolved `opencode_path`, on a clean pass — issue #75
    swaps what was, through issue #74, a bare `OpencodePreflightResult`
    sentinel for this real runner, with no change to this function's own
    `None`-when-off contract or to any existing call site (every one of
    which only ever checked the result for `None`).
    """
    result = preflight(settings, runner=runner)
    if result.opencode_path is None:
        return None
    return OpencodeCliRunner(settings, opencode_path=result.opencode_path)
