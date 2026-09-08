"""`ansina-tui status` — the day-0 smoke test for packaging, config, and the HTTP
client: `GET /healthz` -> `GET /readyz` -> `GET /version`, rendered as one table.

Works with **no token at all** (both health routes are public on the daemon,
`ansina.api.auth.PUBLIC_PATHS`); `/version` answering 401 without a token degrades to
"unknown" rather than an error. The exit code reports the worst finding — see
`ExitCode`'s docstring for the full precedence.
"""

from __future__ import annotations

import os

import typer

from ansina_tui.client import ApiClient, ApiResponse, HostUnreachableError
from ansina_tui.config import (
    InsecureCredentialsFileError,
    load_config,
    load_hosts,
    resolve_host,
    resolve_token,
)
from ansina_tui.context import AppContext
from ansina_tui.exits import ExitCode
from ansina_tui.output import Emitter


def status_command(ctx: typer.Context) -> None:
    """Show daemon health, per-check readiness, and version."""
    app_context: AppContext = ctx.obj
    emitter = Emitter(json_mode=app_context.json_output)

    try:
        config = load_config()
        hosts = load_hosts()
    except InsecureCredentialsFileError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc

    host = resolve_host(app_context.host, os.environ, config)
    token = resolve_token(host, hosts, os.environ)

    with ApiClient(host, token=token) as client:
        try:
            exit_code = _report_status(client, host, emitter)
        except HostUnreachableError as exc:
            emitter.error(str(exc))
            raise typer.Exit(ExitCode.HOST_UNREACHABLE) from exc

    raise typer.Exit(exit_code)


def _report_status(client: ApiClient, host: str, emitter: Emitter) -> ExitCode:
    health = client.get("/healthz")
    ready = client.get("/readyz")
    version = client.get("/version")

    healthy = health.ok
    is_ready = ready.ok
    checks = _readiness_checks(ready)
    version_display = _version_display(version)

    if emitter.json_mode:
        emitter.json(
            {
                "host": host,
                "healthy": healthy,
                "ready": is_ready,
                "checks": checks,
                "version": version_display,
            }
        )
    else:
        rows = [
            ("host", host),
            ("health", "ok" if healthy else "FAILED"),
            ("ready", "ready" if is_ready else "NOT READY"),
        ]
        rows.extend(
            (f"  {name}", "ok" if passing else "fail")
            for name, passing in checks.items()
        )
        rows.append(("version", version_display))
        emitter.rows(rows)

    if not healthy:
        return ExitCode.NOT_HEALTHY
    if not is_ready:
        return ExitCode.NOT_READY
    return ExitCode.OK


def _readiness_checks(ready: ApiResponse) -> dict[str, bool]:
    """The per-check map from `/readyz`'s body. Present as a top-level `checks` key
    either way `/readyz` can answer: the 200 `ReadyStatus` shape carries it directly,
    and the 503 problem shape carries it as an RFC 9457 extension member — which
    lands in the same flat parsed body (`ansina.api.routes.health.readyz`), so one
    read of `json_body` covers both."""
    if isinstance(ready.json_body, dict):
        checks = ready.json_body.get("checks")
        if isinstance(checks, dict):
            return {str(name): bool(passing) for name, passing in checks.items()}
    return {}


def _version_display(version: ApiResponse) -> str:
    if version.ok and isinstance(version.json_body, dict):
        name = version.json_body.get("name", "ansina")
        number = version.json_body.get("version", "unknown")
        return f"{name} {number}"
    return "unknown"
