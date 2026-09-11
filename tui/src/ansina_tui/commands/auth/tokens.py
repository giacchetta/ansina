"""`ansina-tui auth token mint|list|revoke`, over `/auth/me/tokens` (#28). See issue
#32.

Both identities #28 introduced hit `mint`/`revoke` differently than an ordinary user:
the bootstrap identity is refused outright (403 `ansina.auth.bootstrap_identity`,
rendered by `problems.py`'s recognized-message table with the real fix named), and the
configured admin's `POST`/`DELETE /auth/me/tokens` *is* its documented rotation path.
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace

import typer

from ansina_tui.client import ApiResponse, HostUnreachableError
from ansina_tui.config import HostEntry, InsecureCredentialsFileError
from ansina_tui.context import AppContext
from ansina_tui.exits import ExitCode
from ansina_tui.failures import report
from ansina_tui.output import Emitter
from ansina_tui.session import Session, build_client, resolve_session, save_entry


def _require_credential(session: Session, emitter: Emitter) -> None:
    if session.token is None:
        emitter.error(
            f"No credential stored for {session.host}. Run `ansina-tui auth login` "
            "first."
        )
        raise typer.Exit(ExitCode.NOT_AUTHENTICATED)


def mint_command(
    ctx: typer.Context,
    label: str = typer.Option("", "--label", help="A human label for the new token."),
    store: bool | None = typer.Option(
        None,
        "--store/--no-store",
        help="Store the new token as this host's active credential. Prompts when "
        "interactive and omitted.",
    ),
) -> None:
    """Mint a fresh API token via `POST /auth/me/tokens`. The raw token is displayed
    **exactly once** — the same "shown once" framing #24's bootstrap banner uses.
    """
    app_context: AppContext = ctx.obj
    emitter = Emitter(json_mode=app_context.json_output, verbose=app_context.verbose)

    try:
        session = resolve_session(app_context, env=os.environ)
    except InsecureCredentialsFileError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc
    _require_credential(session, emitter)

    try:
        with build_client(session) as client:
            response = client.request(
                "POST", "/auth/me/tokens", json_body={"label": label}
            )
    except HostUnreachableError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.HOST_UNREACHABLE) from exc

    raise typer.Exit(_finish_mint(session, response, emitter, store))


def _finish_mint(
    session: Session, response: ApiResponse, emitter: Emitter, store: bool | None
) -> ExitCode:
    if not response.ok:
        return report(emitter, response)

    body = response.json_body if isinstance(response.json_body, dict) else {}
    token = body.get("token", "")
    token_id = body.get("id", "")
    label = body.get("label", "")

    emitter.line("")
    emitter.line("Your new API token (shown once, then never again):")
    emitter.line("")
    emitter.line(f"  {token}")
    emitter.line("")

    should_store = _resolve_store_choice(session, store)
    if should_store:
        entry = session.hosts.get(session.host, HostEntry())
        save_entry(session, replace(entry, token=token, token_id=token_id))
        emitter.line(f"Stored as the active credential for {session.host}.")

    if emitter.json_mode:
        emitter.json(
            {"id": token_id, "label": label, "token": token, "stored": should_store}
        )
    return ExitCode.OK


def _resolve_store_choice(session: Session, store: bool | None) -> bool:
    if store is not None:
        return store
    if sys.stdin.isatty() and sys.stdout.isatty():
        return typer.confirm(
            f"Store this token as the active credential for {session.host}?",
            default=True,
        )
    return False


def list_command(ctx: typer.Context) -> None:
    """List the caller's own tokens via `GET /auth/me/tokens` — label, created_at,
    last_used_at, never a hash or salt (the daemon never returns one)."""
    app_context: AppContext = ctx.obj
    emitter = Emitter(json_mode=app_context.json_output, verbose=app_context.verbose)

    try:
        session = resolve_session(app_context, env=os.environ)
    except InsecureCredentialsFileError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc
    _require_credential(session, emitter)

    try:
        with build_client(session) as client:
            response = client.get("/auth/me/tokens")
    except HostUnreachableError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.HOST_UNREACHABLE) from exc

    raise typer.Exit(_finish_list(session, response, emitter))


def _token_row(
    item: dict[str, object], current_id: str | None
) -> tuple[str, str, str, str]:
    label = item.get("id", "")
    marker = " (current)" if item.get("id") == current_id else ""
    return (
        f"{label}{marker}",
        str(item.get("label") or "(none)"),
        str(item.get("created_at", "")),
        str(item.get("last_used_at") or "never"),
    )


def _finish_list(session: Session, response: ApiResponse, emitter: Emitter) -> ExitCode:
    if not response.ok:
        return report(emitter, response)

    tokens = response.json_body if isinstance(response.json_body, list) else []
    current_id = session.entry.token_id

    if emitter.json_mode:
        emitter.json({"tokens": tokens})
        return ExitCode.OK

    rows = [_token_row(item, current_id) for item in tokens if isinstance(item, dict)]
    emitter.table(["id", "label", "created_at", "last_used_at"], rows)
    emitter.debug(
        "last_used_at is coalesced server-side and can lag actual use by several "
        "minutes (issue #28)."
    )
    return ExitCode.OK


def revoke_command(
    ctx: typer.Context,
    token_id: str = typer.Argument(..., help="The token id (see `auth token list`)."),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Skip the confirmation prompt when revoking the token currently in use.",
    ),
) -> None:
    """Revoke one token via `DELETE /auth/me/tokens/{id}`. Warns before revoking the
    id currently in use as this host's credential — that would 401 every later
    command until `auth login` runs again."""
    app_context: AppContext = ctx.obj
    emitter = Emitter(json_mode=app_context.json_output, verbose=app_context.verbose)

    try:
        session = resolve_session(app_context, env=os.environ)
    except InsecureCredentialsFileError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc
    _require_credential(session, emitter)

    is_current = session.entry.token_id == token_id
    if is_current and not yes and not _confirm_self_revoke(session, token_id, emitter):
        emitter.line("Aborted.")
        raise typer.Exit(ExitCode.OK)

    try:
        with build_client(session) as client:
            response = client.request("DELETE", f"/auth/me/tokens/{token_id}")
    except HostUnreachableError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.HOST_UNREACHABLE) from exc

    raise typer.Exit(_finish_revoke(session, response, emitter, token_id, is_current))


def _confirm_self_revoke(session: Session, token_id: str, emitter: Emitter) -> bool:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        emitter.error(
            f"{token_id!r} is the token currently in use. Pass --yes to confirm "
            "revoking it non-interactively."
        )
        raise typer.Exit(ExitCode.USAGE)
    return typer.confirm(
        f"{token_id!r} is the token you're currently using against "
        f"{session.host}. Revoke it anyway?",
        default=False,
    )


def _finish_revoke(
    session: Session,
    response: ApiResponse,
    emitter: Emitter,
    token_id: str,
    is_current: bool,
) -> ExitCode:
    if not response.ok:
        return report(emitter, response)

    if is_current:
        entry = session.hosts.get(session.host, HostEntry())
        save_entry(session, replace(entry, token=None, token_id=None))

    emitter.line(f"Revoked token {token_id}.")
    if emitter.json_mode:
        emitter.json({"id": token_id, "revoked": True})
    return ExitCode.OK
