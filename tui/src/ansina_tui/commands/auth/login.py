"""`ansina-tui auth login` / `auth logout`. See issue #32."""

from __future__ import annotations

import os

import typer

from ansina_tui.client import ApiResponse, HostUnreachableError
from ansina_tui.config import HostEntry, InsecureCredentialsFileError
from ansina_tui.context import AppContext
from ansina_tui.exits import ExitCode
from ansina_tui.failures import report
from ansina_tui.output import Emitter
from ansina_tui.secret_input import SecretInputError, read_token
from ansina_tui.session import (
    Session,
    build_client,
    remove_entry,
    resolve_session,
    save_entry,
)

_LOGIN_MESSAGES = {"ansina.unauthorized": "That token was rejected."}


def login_command(
    ctx: typer.Context,
    with_token: bool = typer.Option(
        False,
        "--with-token",
        help="Read the token from stdin instead of a no-echo prompt.",
    ),
) -> None:
    """Validate a token against `GET /auth/me`, then store host + token + the
    returned identity. Stores **nothing** unless the daemon accepts the token.
    The first successful login on a fresh install becomes the default host.
    """
    app_context: AppContext = ctx.obj
    emitter = Emitter(json_mode=app_context.json_output, verbose=app_context.verbose)

    try:
        session = resolve_session(app_context, env=os.environ)
    except InsecureCredentialsFileError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc

    try:
        token = read_token(with_token=with_token)
    except SecretInputError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc

    try:
        with build_client(session, token=token) as client:
            response = client.get("/auth/me")
    except HostUnreachableError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.HOST_UNREACHABLE) from exc

    raise typer.Exit(_finish_login(session, response, emitter, token))


def _finish_login(
    session: Session, response: ApiResponse, emitter: Emitter, token: str
) -> ExitCode:
    if not response.ok:
        return report(emitter, response, messages=_LOGIN_MESSAGES)

    identity = response.json_body if isinstance(response.json_body, dict) else {}
    username = identity.get("username")
    roles = tuple(identity.get("roles", []))
    save_entry(session, HostEntry(token=token, username=username, roles=roles))

    if emitter.json_mode:
        emitter.json(
            {
                "host": session.host,
                "username": username,
                "roles": list(roles),
                "auth_method": identity.get("auth_method"),
            }
        )
    else:
        emitter.line(f"Logged in to {session.host} as {username} ({', '.join(roles)}).")
    return ExitCode.OK


def logout_command(ctx: typer.Context) -> None:
    """Drop the stored credential and sudo grant for this host. Local only — it does
    not revoke the token server-side, since a token you log out of on one machine may
    still be in use on another; `auth token revoke` is the server-side action.
    """
    app_context: AppContext = ctx.obj
    emitter = Emitter(json_mode=app_context.json_output, verbose=app_context.verbose)

    try:
        session = resolve_session(app_context, env=os.environ)
    except InsecureCredentialsFileError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc

    had_entry = session.host in session.hosts
    remove_entry(session)

    if session.token_from_env:
        emitter.warn(
            "ANSINA_TOKEN is set in the environment — logout can't unset it; unset "
            "it yourself if you want this shell fully logged out."
        )
    emitter.line(
        f"Logged out of {session.host}. This is local only — it does not revoke "
        "the token server-side; use `auth token revoke` for that."
    )
    if emitter.json_mode:
        emitter.json({"host": session.host, "removed": had_entry})
    raise typer.Exit(ExitCode.OK)
