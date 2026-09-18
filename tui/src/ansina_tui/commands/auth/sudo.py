"""`ansina-tui auth sudo`. See issue #32 and #26 (the grant/TTL/lockout semantics this
wraps), generalized to multiple step-up factors by issue #44 (client side of #37/#41).
The bootstrap identity and the configured admin (#28) are both password-less and hold
`admin`, which never needs sudo — a step-up attempt from either fails with a 401 the
daemon means literally ("wrong password"/"wrong code"), which `_step_up_messages`
renders plainly rather than pointing at `auth login`.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import typer

from ansina_tui.client import ApiClient, ApiResponse, HostUnreachableError
from ansina_tui.commands.auth.common import require_credential
from ansina_tui.config import HostEntry, InsecureCredentialsFileError
from ansina_tui.context import AppContext
from ansina_tui.exits import ExitCode
from ansina_tui.failures import report
from ansina_tui.output import Emitter
from ansina_tui.secret_input import SecretInputError, read_password, read_totp_code
from ansina_tui.session import (
    Session,
    build_client,
    resolve_session,
    save_entry,
    sudo_state,
)


@dataclass(frozen=True, slots=True)
class _Factor:
    """One step-up factor this client knows how to prompt for. Deliberately not
    shared with `ansina.auth.step_up.StepUpVerifier` — `tui/` never imports `ansina`
    (see `README.md`) — so this is a small, hand-copied, client-side mirror of the two
    verifiers the daemon ships as of issue #41, the same discipline `problems.py`'s
    own hand-copied code table already follows.
    """

    name: str
    payload_key: str
    label: str  # used in prompts and in the per-factor "verification failed" message


_FACTORS = {
    "password": _Factor(name="password", payload_key="password", label="Password"),
    "totp": _Factor(name="totp", payload_key="code", label="TOTP code"),
}


def _read_secret(factor: _Factor) -> str:
    if factor.name == "totp":
        return read_totp_code()
    return read_password()


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
    factor: str | None = typer.Option(
        None,
        "--factor",
        help="Which enrolled step-up factor to use (password|totp). Only needed "
        "when 2+ are enrolled, or to skip the GET /auth/me lookup non-interactively.",
    ),
) -> None:
    app_context: AppContext = ctx.obj
    emitter = Emitter(json_mode=app_context.json_output, verbose=app_context.verbose)

    if revoke and status:
        emitter.error("--revoke and --status are mutually exclusive.")
        raise typer.Exit(ExitCode.USAGE)

    if factor is not None and factor not in _FACTORS:
        emitter.error(
            f"Unknown factor {factor!r}. Known factors: {', '.join(sorted(_FACTORS))}."
        )
        raise typer.Exit(ExitCode.USAGE)

    try:
        session = resolve_session(app_context, env=os.environ)
    except InsecureCredentialsFileError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc

    if status:
        raise typer.Exit(_report_status_only(session, emitter))

    require_credential(session, emitter)

    if revoke:
        raise typer.Exit(_revoke(session, emitter))

    raise typer.Exit(_step_up(session, emitter, factor))


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


def _resolve_factor(
    client: ApiClient, emitter: Emitter, requested: str | None
) -> _Factor | ExitCode:
    """Picks which enrolled factor to step up with, or returns the `ExitCode` to
    exit with when it can't. `requested` (`--factor`) skips the `GET /auth/me`
    round-trip entirely — an unenrolled name is left for the daemon's own 403
    `ansina.auth.step_up_unavailable` to answer, keeping the CI (`api`-style,
    non-interactive) path at one request.
    """
    if requested is not None:
        return _FACTORS[requested]

    response = client.get("/auth/me")
    if not response.ok:
        return report(emitter, response)

    body = response.json_body if isinstance(response.json_body, dict) else {}
    raw_factors = body.get("step_up_factors", [])
    enrolled = (
        [f for f in raw_factors if isinstance(f, str)]
        if isinstance(raw_factors, list)
        else []
    )

    if not enrolled:
        emitter.error(
            "No step-up factor is enrolled for this account. Run "
            "`ansina-tui auth totp enroll` first."
        )
        return ExitCode.FORBIDDEN

    known = [_FACTORS[f] for f in enrolled if f in _FACTORS]
    if not known:
        emitter.error(
            f"Enrolled factor(s) {', '.join(enrolled)!r} aren't recognized by this "
            "client. Pass --factor explicitly, or use `ansina-tui api` directly."
        )
        return ExitCode.USAGE

    if len(known) == 1:
        return known[0]

    return _disambiguate(known, emitter)


def _disambiguate(known: list[_Factor], emitter: Emitter) -> _Factor | ExitCode:
    names = [f.name for f in known]
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        emitter.error(
            f"Multiple step-up factors are enrolled ({', '.join(names)}). Pass "
            "--factor to choose one non-interactively."
        )
        return ExitCode.USAGE

    choice = typer.prompt(f"Which factor? ({'/'.join(names)})")
    for candidate in known:
        if candidate.name == choice:
            return candidate
    emitter.error(f"Unknown factor {choice!r}.")
    return ExitCode.USAGE


def _step_up(session: Session, emitter: Emitter, requested: str | None) -> ExitCode:
    with build_client(session) as client:
        try:
            factor = _resolve_factor(client, emitter, requested)
        except HostUnreachableError as exc:
            emitter.error(str(exc))
            raise typer.Exit(ExitCode.HOST_UNREACHABLE) from exc
        if isinstance(factor, ExitCode):
            return factor

        try:
            secret = _read_secret(factor)
        except SecretInputError as exc:
            emitter.error(str(exc))
            raise typer.Exit(ExitCode.USAGE) from exc

        try:
            response = client.request(
                "POST",
                "/auth/sudo",
                json_body={"factor": factor.name, factor.payload_key: secret},
            )
        except HostUnreachableError as exc:
            emitter.error(str(exc))
            raise typer.Exit(ExitCode.HOST_UNREACHABLE) from exc

    return _finish_step_up(session, response, emitter, factor)


def _finish_step_up(
    session: Session, response: ApiResponse, emitter: Emitter, factor: _Factor
) -> ExitCode:
    if not response.ok:
        messages = {"ansina.unauthorized": f"{factor.label} verification failed."}
        return report(emitter, response, messages=messages)

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
