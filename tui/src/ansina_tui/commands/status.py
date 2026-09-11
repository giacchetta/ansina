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

from ansina_tui.client import ApiClient, HostUnreachableError
from ansina_tui.config import InsecureCredentialsFileError
from ansina_tui.context import AppContext
from ansina_tui.daemon_state import readiness_checks, version_display
from ansina_tui.exits import ExitCode
from ansina_tui.output import Emitter
from ansina_tui.session import build_client, resolve_session


def status_command(ctx: typer.Context) -> None:
    """Show daemon health, per-check readiness, and version."""
    app_context: AppContext = ctx.obj
    emitter = Emitter(json_mode=app_context.json_output, verbose=app_context.verbose)

    try:
        session = resolve_session(app_context, env=os.environ)
    except InsecureCredentialsFileError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc

    with build_client(session) as client:
        try:
            exit_code = _report_status(client, session.host, emitter)
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
    checks = readiness_checks(ready)
    version_text = version_display(version)

    if emitter.json_mode:
        emitter.json(
            {
                "host": host,
                "healthy": healthy,
                "ready": is_ready,
                "checks": checks,
                "version": version_text,
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
        rows.append(("version", version_text))
        emitter.rows(rows)

    if not healthy:
        return ExitCode.NOT_HEALTHY
    if not is_ready:
        return ExitCode.NOT_READY
    return ExitCode.OK
