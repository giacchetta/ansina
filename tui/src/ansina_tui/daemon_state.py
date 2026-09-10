"""What the daemon says about itself, as data — the shared read model behind
`ansina-tui status` and the Overview tab (issue #34).

`fetch_overview` is a **pure synchronous function with no Textual involvement at
all** — every state it can produce is unit-tested against `httpx.MockTransport` with
no `App` in sight, which is what makes the TUI's 100% coverage bar reachable without
fighting the pilot. It deliberately **never raises**: `HostUnreachableError` and
`InsecureCredentialsFileError` both become an `OverviewSnapshot` with `error` set,
mirroring the same invariant `ansina.brain.BrainProvider.stream()` keeps on the daemon
side — load-bearing here because Textual's `@work` defaults to `exit_on_error=True`,
so a raise out of a worker would tear the whole app down instead of rendering a
designed empty state.

Builds its client through `session.resolve_session()`/`session.build_client()` — the
same single construction point every CLI command uses, and therefore the same
`patch_transport` test seam (`tests/conftest.py`) — re-resolved on every call, so a
credential change made by another command (or another terminal) is picked up on the
next refresh with no restart.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from ansina_tui.client import ApiClient, ApiResponse, HostUnreachableError
from ansina_tui.config import InsecureCredentialsFileError, load_config, resolve_host
from ansina_tui.context import AppContext
from ansina_tui.session import Session, build_client, resolve_session


@dataclass(frozen=True, slots=True)
class Panel:
    """One Overview section. `rows` is the label/value data to show; `message` is a
    designed, human-readable line — an error, or a note explaining why there's
    nothing to show — never a traceback. A panel may carry both (e.g. `/readyz`'s
    per-check rows alongside its "not ready" detail) or neither (rendered as a plain
    placeholder by the caller)."""

    rows: tuple[tuple[str, str], ...] = ()
    message: str | None = None


@dataclass(frozen=True, slots=True)
class OverviewSnapshot:
    """One full read of the daemon's self-reported state."""

    host: str
    connection: str  # "connected" | "unreachable" | "config error"
    error: str | None  # a whole-tab failure (unreachable host, insecure hosts.toml)
    health: Panel
    readiness: Panel
    version: Panel
    identity: Panel


def readiness_checks(ready: ApiResponse) -> dict[str, bool]:
    """The per-check map from `/readyz`'s body. Present as a top-level `checks` key
    either way `/readyz` can answer: the 200 `ReadyStatus` shape carries it directly,
    and the 503 problem shape carries it as an RFC 9457 extension member — which
    lands in the same flat parsed body (`ansina.api.routes.health.readyz`), so one
    read of `json_body` covers both. Shared by `commands/status.py` and this module
    so the CLI and the TUI agree on exactly what a "check" is."""
    if isinstance(ready.json_body, dict):
        checks = ready.json_body.get("checks")
        if isinstance(checks, dict):
            return {str(name): bool(passing) for name, passing in checks.items()}
    return {}


def version_display(version: ApiResponse) -> str:
    """`"<name> <version>"` on success, else `"unknown"` — shared with
    `commands/status.py`, which is the reason a token-less `/version` 401 degrades to
    `"unknown"` there rather than an error."""
    if version.ok and isinstance(version.json_body, dict):
        name = version.json_body.get("name", "ansina")
        number = version.json_body.get("version", "unknown")
        return f"{name} {number}"
    return "unknown"


def _failure_message(response: ApiResponse) -> str:
    if response.problem is not None:
        return response.problem.message
    return f"Request failed (HTTP {response.status_code})."


def _health_panel(health: ApiResponse) -> Panel:
    if health.ok:
        return Panel(rows=(("status", "ok"),))
    return Panel(message=_failure_message(health))


def _readiness_panel(ready: ApiResponse) -> Panel:
    checks = readiness_checks(ready)
    rows = tuple(
        (name, "ok" if passing else "fail") for name, passing in checks.items()
    )
    if ready.ok:
        return Panel(rows=rows)
    return Panel(rows=rows, message=_failure_message(ready))


def _version_panel(version: ApiResponse) -> Panel:
    if version.ok:
        return Panel(rows=(("version", version_display(version)),))
    if version.problem is not None:
        return Panel(message=version.problem.message)
    return Panel(rows=(("version", "unknown"),))


def _identity_panel(session: Session, client: ApiClient) -> Panel:
    if session.token is None:
        return Panel(
            message=(
                f"No credential stored for {session.host}. Run "
                "`ansina-tui auth login`, or set ANSINA_TOKEN."
            )
        )

    response = client.get("/auth/me")
    if not response.ok:
        return Panel(message=_failure_message(response))

    identity = response.json_body if isinstance(response.json_body, dict) else {}
    username = identity.get("username", "")
    roles = identity.get("roles", [])
    auth_method = identity.get("auth_method", "")
    sudo_active = identity.get("sudo_active", False)
    rows = (
        ("username", str(username)),
        ("roles", ", ".join(roles) if isinstance(roles, list) else str(roles)),
        ("auth method", str(auth_method)),
        ("sudo", "active" if sudo_active else "inactive"),
    )
    return Panel(rows=rows)


_EMPTY_PANEL = Panel()


def fetch_overview(
    app_context: AppContext, *, env: Mapping[str, str] | None = None
) -> OverviewSnapshot:
    """One full read for the Overview tab: `/healthz`, `/readyz`, `/version`, and
    (with a stored credential) `/auth/me`. Never raises — see the module docstring."""
    resolved_env = env if env is not None else os.environ

    try:
        session = resolve_session(app_context, env=resolved_env)
    except InsecureCredentialsFileError as exc:
        host = resolve_host(app_context.host, resolved_env, load_config())
        return OverviewSnapshot(
            host=host,
            connection="config error",
            error=str(exc),
            health=_EMPTY_PANEL,
            readiness=_EMPTY_PANEL,
            version=_EMPTY_PANEL,
            identity=_EMPTY_PANEL,
        )

    try:
        with build_client(session) as client:
            health = client.get("/healthz")
            ready = client.get("/readyz")
            version = client.get("/version")
            identity = _identity_panel(session, client)
    except HostUnreachableError as exc:
        return OverviewSnapshot(
            host=session.host,
            connection="unreachable",
            error=str(exc),
            health=_EMPTY_PANEL,
            readiness=_EMPTY_PANEL,
            version=_EMPTY_PANEL,
            identity=_EMPTY_PANEL,
        )

    return OverviewSnapshot(
        host=session.host,
        connection="connected",
        error=None,
        health=_health_panel(health),
        readiness=_readiness_panel(ready),
        version=_version_panel(version),
        identity=identity,
    )
