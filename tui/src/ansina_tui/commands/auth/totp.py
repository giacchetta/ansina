"""`ansina-tui auth totp enroll|status|disable`, over `/auth/me/totp` (#41). See issue
#44 — the client-side counterpart to #37's multi-factor `StepUpRegistry` and #41's TOTP
verifier; without this, obtaining a factor at all meant the raw `ansina-tui api -X
POST /auth/me/totp` escape hatch, and the result was never usable by `auth sudo`
because nothing persists it (TOTP has no local secret to store — only the daemon holds
the encrypted envelope, unlike a sudo grant or a bearer token).
"""

from __future__ import annotations

import os

import typer

from ansina_tui.client import ApiResponse, HostUnreachableError
from ansina_tui.commands.auth.common import require_credential
from ansina_tui.config import InsecureCredentialsFileError
from ansina_tui.context import AppContext
from ansina_tui.exits import ExitCode
from ansina_tui.failures import report
from ansina_tui.output import Emitter
from ansina_tui.session import Session, build_client, resolve_session


def enroll_command(ctx: typer.Context) -> None:
    """Enroll a TOTP secret via `POST /auth/me/totp`. The `otpauth://` URI and base32
    secret are displayed **exactly once** — the same "shown once" framing
    `auth token mint` already uses — and never written to `hosts.toml` or any log:
    there is nothing local to store, since verification lives entirely server-side.
    """
    app_context: AppContext = ctx.obj
    emitter = Emitter(json_mode=app_context.json_output, verbose=app_context.verbose)

    try:
        session = resolve_session(app_context, env=os.environ)
    except InsecureCredentialsFileError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc
    require_credential(session, emitter)

    try:
        with build_client(session) as client:
            response = client.request("POST", "/auth/me/totp")
    except HostUnreachableError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.HOST_UNREACHABLE) from exc

    raise typer.Exit(_finish_enroll(response, emitter))


def _finish_enroll(response: ApiResponse, emitter: Emitter) -> ExitCode:
    if not response.ok:
        return report(emitter, response)

    body = response.json_body if isinstance(response.json_body, dict) else {}
    secret = str(body.get("secret", ""))
    uri = str(body.get("otpauth_uri", ""))

    emitter.line("")
    emitter.line("Your new TOTP secret (shown once, then never again):")
    emitter.line("")
    emitter.line(f"  {secret}")
    emitter.line("")
    emitter.line(f"Add it to your authenticator app, or scan/enter this URI: {uri}")
    emitter.line("")

    if emitter.json_mode:
        emitter.json(
            {
                "secret": secret,
                "otpauth_uri": uri,
                "digits": body.get("digits"),
                "period_seconds": body.get("period_seconds"),
            }
        )
    return ExitCode.OK


def totp_status_command(ctx: typer.Context) -> None:
    """Enrollment status via `GET /auth/me/totp` — never a secret, just whether one
    exists and since when."""
    app_context: AppContext = ctx.obj
    emitter = Emitter(json_mode=app_context.json_output, verbose=app_context.verbose)

    try:
        session = resolve_session(app_context, env=os.environ)
    except InsecureCredentialsFileError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc
    require_credential(session, emitter)

    try:
        with build_client(session) as client:
            response = client.get("/auth/me/totp")
    except HostUnreachableError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.HOST_UNREACHABLE) from exc

    raise typer.Exit(_finish_status(response, emitter))


def _finish_status(response: ApiResponse, emitter: Emitter) -> ExitCode:
    if not response.ok:
        return report(emitter, response)

    body = response.json_body if isinstance(response.json_body, dict) else {}
    enrolled = bool(body.get("enrolled"))
    enrolled_since = body.get("enrolled_since")

    if emitter.json_mode:
        emitter.json({"enrolled": enrolled, "enrolled_since": enrolled_since})
    else:
        emitter.rows(
            [
                ("enrolled", "yes" if enrolled else "no"),
                ("enrolled_since", str(enrolled_since or "—")),
            ]
        )
    return ExitCode.OK


def disable_command(ctx: typer.Context) -> None:
    """Disable TOTP via `DELETE /auth/me/totp`. Requires a live sudo grant — a
    missing one surfaces the daemon's 403 `ansina.auth.sudo_required` through the
    ordinary `failures.report()` path, unchanged; no local confirmation prompt, since
    the sudo requirement is already the gate against an unattended stolen token doing
    this. Idempotent server-side (204 whether or not a credential existed)."""
    app_context: AppContext = ctx.obj
    emitter = Emitter(json_mode=app_context.json_output, verbose=app_context.verbose)

    try:
        session = resolve_session(app_context, env=os.environ)
    except InsecureCredentialsFileError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc
    require_credential(session, emitter)

    try:
        with build_client(session) as client:
            response = client.request("DELETE", "/auth/me/totp")
    except HostUnreachableError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.HOST_UNREACHABLE) from exc

    raise typer.Exit(_finish_disable(session, response, emitter))


def _finish_disable(
    session: Session, response: ApiResponse, emitter: Emitter
) -> ExitCode:
    if not response.ok:
        return report(emitter, response)

    emitter.line(f"TOTP disabled for {session.host}.")
    if emitter.json_mode:
        emitter.json({"enrolled": False})
    return ExitCode.OK
