"""`ansina-tui auth sudo`. See issue #32 and #26 (the grant/TTL/lockout semantics this
wraps). The bootstrap identity and the configured admin (#28) are both password-less
and hold `admin`, which never needs sudo — a step-up attempt from either fails with a
401 the daemon means literally ("wrong password"), which `_STEP_UP_MESSAGES` renders
plainly rather than pointing at `auth login`.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime

import typer

from ansina_tui.client import ApiResponse, HostUnreachableError
from ansina_tui.config import HostEntry, InsecureCredentialsFileError
from ansina_tui.context import AppContext
from ansina_tui.exits import ExitCode
from ansina_tui.failures import report
from ansina_tui.output import Emitter
from ansina_tui.secret_input import SecretInputError, read_password
from ansina_tui.session import (
    Session,
    build_client,
    resolve_session,
    save_entry,
    sudo_state,
)

_STEP_UP_MESSAGES = {"ansina.unauthorized": "Password verification failed."}


def _now() -> datetime:
    """Its own function, monkeypatched directly by tests — the same pattern
    `main.py`'s `_is_interactive` uses — rather than a `datetime.now(UTC)` call
    inline, or a `= datetime.now(UTC)` default argument (bound once at import time,
    which is exactly the trap `secret_input.py`'s own module docstring documents)."""
    return datetime.now(UTC)


def sudo_command(
    ctx: typer.Context,
    revoke: bool = typer.Option(
        False, "--revoke", help="Revoke the current grant, server-side and locally."
    ),
    status: bool = typer.Option(
        False,
        "--status",
        help="Report remaining grant time without touching the server.",
    ),
) -> None:
    app_context: AppContext = ctx.obj
    emitter = Emitter(json_mode=app_context.json_output, verbose=app_context.verbose)

    if revoke and status:
        emitter.error("--revoke and --status are mutually exclusive.")
        raise typer.Exit(ExitCode.USAGE)

    try:
        session = resolve_session(app_context, env=os.environ)
    except InsecureCredentialsFileError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc

    if status:
        raise typer.Exit(_report_status_only(session, emitter))

    if session.token is None:
        emitter.error(
            f"No credential stored for {session.host}. Run `ansina-tui auth login` "
            "first."
        )
        raise typer.Exit(ExitCode.NOT_AUTHENTICATED)

    if revoke:
        raise typer.Exit(_revoke(session, emitter))

    raise typer.Exit(_step_up(session, emitter))


def _report_status_only(session: Session, emitter: Emitter) -> ExitCode:
    """Local only — never touches the server. A grant found expired here is also
    dropped from disk (self-healing): the one deliberate write this branch makes."""
    raw_entry = session.hosts.get(session.host, HostEntry())
    grant = sudo_state(raw_entry, _now())

    if grant.state == "expired":
        save_entry(session, replace(raw_entry, sudo_token=None, sudo_expires_at=None))

    if emitter.json_mode:
        emitter.json(
            {
                "sudo": grant.state,
                "expires_at": grant.expires_at.isoformat()
                if grant.expires_at
                else None,
            }
        )
    elif grant.state == "active" and grant.expires_at:
        emitter.line(f"sudo: active (expires {grant.expires_at.isoformat()})")
    elif grant.state == "expired":
        emitter.line("sudo: none (previous grant expired)")
    else:
        emitter.line("sudo: none")
    return ExitCode.OK


def _revoke(session: Session, emitter: Emitter) -> ExitCode:
    try:
        with build_client(session) as client:
            response = client.request("DELETE", "/auth/sudo")
    except HostUnreachableError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.HOST_UNREACHABLE) from exc

    return _finish_revoke(session, response, emitter)


def _finish_revoke(
    session: Session, response: ApiResponse, emitter: Emitter
) -> ExitCode:
    if not response.ok:
        return report(emitter, response)

    entry = session.hosts.get(session.host, HostEntry())
    save_entry(session, replace(entry, sudo_token=None, sudo_expires_at=None))
    emitter.line(f"Sudo grant revoked for {session.host}.")
    if emitter.json_mode:
        emitter.json({"sudo": "none"})
    return ExitCode.OK


def _step_up(session: Session, emitter: Emitter) -> ExitCode:
    try:
        password = read_password()
    except SecretInputError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc

    try:
        with build_client(session) as client:
            response = client.request(
                "POST", "/auth/sudo", json_body={"password": password}
            )
    except HostUnreachableError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.HOST_UNREACHABLE) from exc

    return _finish_step_up(session, response, emitter)


def _finish_step_up(
    session: Session, response: ApiResponse, emitter: Emitter
) -> ExitCode:
    if not response.ok:
        return report(emitter, response, messages=_STEP_UP_MESSAGES)

    body = response.json_body if isinstance(response.json_body, dict) else {}
    expires_at = body.get("expires_at")
    entry = session.hosts.get(session.host, HostEntry())
    save_entry(
        session,
        replace(entry, sudo_token=body.get("token"), sudo_expires_at=expires_at),
    )

    emitter.line(f"Sudo granted for {session.host}, expires {expires_at}.")
    if emitter.json_mode:
        emitter.json({"sudo": "active", "expires_at": expires_at})
    return ExitCode.OK
