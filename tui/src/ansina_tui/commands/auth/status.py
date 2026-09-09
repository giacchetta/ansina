"""`ansina-tui auth status`. See issue #32.

Always makes a live `GET /auth/me` call — there is deliberately no cache fallback
(`hosts.toml`'s cached username/roles are only what `auth login` last observed, not
necessarily current). An unreachable host exits 5, the same as every other command —
one code path, matching `session.py`'s own resolution.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import typer

from ansina_tui.client import ApiResponse, HostUnreachableError
from ansina_tui.config import HostEntry, InsecureCredentialsFileError
from ansina_tui.context import AppContext
from ansina_tui.exits import ExitCode
from ansina_tui.failures import report
from ansina_tui.output import Emitter
from ansina_tui.session import Session, build_client, resolve_session, sudo_state


def _now() -> datetime:
    """Its own function, monkeypatched directly by tests — the same pattern
    `main.py`'s `_is_interactive` uses — rather than a `datetime.now(UTC)` call
    inline, or a `= datetime.now(UTC)` default argument (bound once at import time,
    which is exactly the trap `secret_input.py`'s own module docstring documents)."""
    return datetime.now(UTC)


def auth_status_command(ctx: typer.Context) -> None:
    """Host, credential source, identity, and sudo state — never the token itself;
    there is deliberately no `--show-token`."""
    app_context: AppContext = ctx.obj
    emitter = Emitter(json_mode=app_context.json_output, verbose=app_context.verbose)

    try:
        session = resolve_session(app_context, env=os.environ)
    except InsecureCredentialsFileError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc

    if session.token is None:
        emitter.error(
            f"No credential stored for {session.host}. Run `ansina-tui auth login`, "
            "or set ANSINA_TOKEN."
        )
        raise typer.Exit(ExitCode.NOT_AUTHENTICATED)

    try:
        with build_client(session) as client:
            response = client.get("/auth/me")
    except HostUnreachableError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.HOST_UNREACHABLE) from exc

    raise typer.Exit(_finish_status(session, response, emitter))


def _finish_status(
    session: Session, response: ApiResponse, emitter: Emitter
) -> ExitCode:
    if not response.ok:
        return report(emitter, response)

    identity = response.json_body if isinstance(response.json_body, dict) else {}
    username = identity.get("username", "")
    roles = identity.get("roles", [])
    auth_method = identity.get("auth_method", "")

    # The raw, unscrubbed entry (not `session.entry`) so an *expired* grant renders
    # as "expired" rather than indistinguishable from "none" — see `session.py`'s
    # own `Session.entry` docstring.
    raw_entry = session.hosts.get(session.host, HostEntry())
    grant = sudo_state(raw_entry, _now())
    credential_source = "ANSINA_TOKEN" if session.token_from_env else "stored"

    if emitter.json_mode:
        emitter.json(
            {
                "host": session.host,
                "credential_source": credential_source,
                "username": username,
                "roles": roles,
                "auth_method": auth_method,
                "sudo": grant.state,
                "sudo_expires_at": grant.expires_at.isoformat()
                if grant.expires_at
                else None,
            }
        )
    else:
        sudo_display = (
            f"active (expires {grant.expires_at.isoformat()})"
            if grant.state == "active" and grant.expires_at
            else grant.state
        )
        emitter.rows(
            [
                ("host", session.host),
                ("credential", credential_source),
                ("username", username),
                ("roles", ", ".join(roles)),
                ("auth method", auth_method),
                ("sudo", sudo_display),
            ]
        )
    return ExitCode.OK
