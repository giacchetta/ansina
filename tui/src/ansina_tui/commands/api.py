"""`ansina-tui api` — a raw REST wrapper reaching any daemon route with no per-route
special-casing and no allow-list to keep in sync (issue #33): the reason M4 can ship
only three CLI commands (`status`, `auth`, `api`) without leaving anything unreachable,
and the milestone's CI/CD surface — so it must never block for input.

`curl` ergonomics without the `curl` ceremony: auth is automatic (`session.build_client`
already attaches the stored bearer token and a live sudo grant), so the sudo dance —
`POST /auth/sudo`, extract the grant, paste it into the next call — disappears.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

import typer

from ansina_tui.client import RESERVED_HEADERS, ApiResponse, HostUnreachableError
from ansina_tui.config import InsecureCredentialsFileError
from ansina_tui.context import AppContext
from ansina_tui.exits import ExitCode
from ansina_tui.failures import report
from ansina_tui.output import Emitter
from ansina_tui.session import build_client, resolve_session


def _stdout_is_tty() -> bool:
    """Its own function, not `sys.stdout.isatty()` inlined — the same reason
    `main._is_interactive` and `commands/auth/sudo._now` exist as functions:
    `typer.testing.CliRunner` swaps `sys.stdout` for the duration of an `invoke()`
    call, so a test patches this function rather than the stream itself."""
    return sys.stdout.isatty()


def api_command(
    ctx: typer.Context,
    path: str = typer.Argument(
        ..., help="Route path, e.g. /version or /auth/me. Never a full URL."
    ),
    method: str | None = typer.Option(
        None,
        "-X",
        "--method",
        help="HTTP method. Defaults to POST when a body is supplied via -f/--input, "
        "else GET.",
    ),
    fields: list[str] = typer.Option(
        [],
        "-f",
        "--field",
        help="key=value, repeatable, assembled into a JSON object body. Values are "
        "always JSON strings — for a typed field (bool/number/nested), use --input "
        "instead. Mutually exclusive with --input.",
    ),
    input_path: str | None = typer.Option(
        None,
        "--input",
        help="Raw request body read from a file, or - for stdin. Mutually exclusive "
        "with -f/--field.",
    ),
    raw_headers: list[str] = typer.Option(
        [],
        "-H",
        "--header",
        help="'Name: value', repeatable. Merged over — never replacing — the auth "
        "headers the client attaches.",
    ),
    include: bool = typer.Option(
        False,
        "-i",
        "--include",
        help="Prepend the status line and response headers. Suppressed under --json "
        "or a piped stdout, same as every other human-only rendering.",
    ),
) -> None:
    """Reach any daemon route directly — no per-route special-casing, no allow-list."""
    app_context: AppContext = ctx.obj
    json_mode = app_context.json_output or not _stdout_is_tty()
    emitter = Emitter(json_mode=json_mode, verbose=app_context.verbose)

    if fields and input_path is not None:
        emitter.error("-f/--field and --input are mutually exclusive.")
        raise typer.Exit(ExitCode.USAGE)

    if "://" in path:
        emitter.error(
            "path must be a route path (e.g. /version), not a full URL — it would "
            "bypass --host and send the stored credential to an arbitrary host."
        )
        raise typer.Exit(ExitCode.USAGE)
    normalized_path = path if path.startswith("/") else f"/{path}"

    try:
        headers = _parse_headers(raw_headers)
    except ValueError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc

    json_body: dict[str, str] | None = None
    content: str | None = None
    if fields:
        try:
            json_body = _parse_fields(fields)
        except ValueError as exc:
            emitter.error(str(exc))
            raise typer.Exit(ExitCode.USAGE) from exc
    elif input_path is not None:
        try:
            content = _read_input(input_path)
        except OSError as exc:
            emitter.error(f"Could not read {input_path}: {exc}")
            raise typer.Exit(ExitCode.USAGE) from exc
        if not _has_header(headers, "content-type"):
            headers["Content-Type"] = "application/json"

    for name in headers:
        if name.lower() in RESERVED_HEADERS:
            emitter.warn(
                f"-H {name!r} is ignored — Authorization/X-Sudo-Token are always set "
                "from the stored credential/sudo grant, never overridable."
            )

    has_body = json_body is not None or content is not None
    resolved_method = (method or ("POST" if has_body else "GET")).upper()

    try:
        session = resolve_session(app_context, env=os.environ)
    except InsecureCredentialsFileError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc

    try:
        with build_client(session) as client:
            response = client.request(
                resolved_method,
                normalized_path,
                json_body=json_body,
                content=content,
                headers=headers,
            )
    except HostUnreachableError as exc:
        emitter.error(str(exc))
        raise typer.Exit(ExitCode.HOST_UNREACHABLE) from exc

    raise typer.Exit(_finish(emitter, response, include=include))


def _finish(emitter: Emitter, response: ApiResponse, *, include: bool) -> ExitCode:
    if include:
        emitter.line(f"HTTP {response.status_code}")
        for name, value in response.headers.items():
            emitter.line(f"{name}: {value}")
        emitter.line("")

    # Written whether or not the response was 2xx — a problem+json body pipes into
    # `jq` exactly like a success body does.
    emitter.body(_render_body(response.text, pretty=not emitter.json_mode))

    if not response.ok:
        return report(emitter, response)
    return ExitCode.OK


def _render_body(text: str, *, pretty: bool) -> str:
    """Pretty-printed on a TTY with no --json; raw (exactly as the daemon sent it)
    under --json or a piped stdout, and whenever the body isn't JSON at all."""
    if not pretty or not text:
        return text
    try:
        parsed = json.loads(text)
    except ValueError:
        return text
    return json.dumps(parsed, indent=2)


def _parse_fields(fields: list[str]) -> dict[str, str]:
    body: dict[str, str] = {}
    for field in fields:
        key, sep, value = field.partition("=")
        if not sep or not key:
            raise ValueError(f"-f/--field must be key=value, got {field!r}.")
        body[key] = value
    return body


def _parse_headers(raw_headers: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw in raw_headers:
        name, sep, value = raw.partition(":")
        name = name.strip()
        if not sep or not name:
            raise ValueError(f"-H/--header must be 'Name: value', got {raw!r}.")
        headers[name] = value.strip()
    return headers


def _has_header(headers: Mapping[str, str], name: str) -> bool:
    return any(existing.lower() == name for existing in headers)


def _read_input(input_path: str) -> str:
    if input_path == "-":
        return sys.stdin.read()
    return Path(input_path).read_text(encoding="utf-8")
