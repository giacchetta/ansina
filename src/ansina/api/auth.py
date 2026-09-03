"""Bearer-token enforcement — the internal API boundary from issue #5, extended by
issue #24 to check the RBAC identity model's `credentials` table instead of a single
static config secret, by issue #25 to resolve a full `Principal` (not just "some
identity exists") through a formal `Authenticator` chain, and by issue #26 to elevate
that `Principal` via an `X-Sudo-Token` header carrying a live sudo grant.

Pure ASGI middleware (`__call__(scope, receive, send)`), not
`starlette.BaseHTTPMiddleware`, for the same reason `RequestIdMiddleware` is: that class
buffers the response and runs the downstream handler in a separate task, which breaks
nesting `contextvars.ContextVar.set()`/`.reset()` cleanly around streaming responses.

Deny-by-default: every route requires a token except `PUBLIC_PATHS`. A route added by
a later milestone is protected automatically, with no per-route opt-in to forget.

Builds the `problem+json` response itself rather than raising — the exception handlers
registered on the `FastAPI` app sit *inside* the middleware stack, so a raised exception
here would escape past them and reach the client as a bare, framework-default 500.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio.to_thread

from ansina.api.problems import CODE_UNAUTHORIZED, problem_response
from ansina.auth.authenticator import build_authenticators, resolve_principal

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from ansina.auth.authenticator import Authenticator
    from ansina.auth.sudo import SudoService
    from ansina.storage.database import Database

# Reachable with no token, even when auth is enabled. Everything else is
# deny-by-default, including /version and the OpenAPI/docs routes.
PUBLIC_PATHS = frozenset({"/healthz", "/readyz"})

_HEADER_NAME = b"authorization"
_SCHEME = "bearer"
_WWW_AUTHENTICATE = "Bearer"
_SUDO_HEADER_NAME = b"x-sudo-token"


def _extract_bearer_token(scope: Scope) -> str | None:
    """The credential from an `Authorization: Bearer <token>` header, or `None` if the
    header is absent, repeated, or not a well-formed bearer credential.
    """
    header: bytes | None = None
    for key, value in scope.get("headers", ()):
        if key == _HEADER_NAME:
            header = value
            break
    if header is None:
        return None
    try:
        decoded = header.decode("ascii")
    except UnicodeDecodeError:
        return None
    scheme, _, token = decoded.partition(" ")
    if scheme.lower() != _SCHEME or not token:
        return None
    return token


def _extract_sudo_token(scope: Scope) -> str | None:
    """The raw `X-Sudo-Token` header value, or `None` if absent/malformed — issue
    #26's step-up grant, unlike the bearer token, carries no scheme prefix to parse.
    """
    for key, value in scope.get("headers", ()):
        if key == _SUDO_HEADER_NAME:
            try:
                return value.decode("ascii") or None
            except UnicodeDecodeError:
                return None
    return None


class BearerAuthMiddleware:
    """Rejects any non-`PUBLIC_PATHS` request that doesn't carry a currently-active
    credential, and resolves the matched identity into a full `Principal` (issue #25)
    attached to `scope["state"]["principal"]` for `ansina.api.authorization.require()`
    to read back — every route past this middleware runs with a `Principal` already on
    request state, or never runs at all.

    `enabled=False` (`Settings.security.enabled`) disables authentication outright —
    every request passes through with no `Principal` resolved. `config.settings.
    Settings` refuses to construct at all when that's paired with a non-loopback bind
    (`Settings._refuse_unsafe_bind`), so a disabled-auth app is never reachable off the
    local machine.

    Verification runs through an `Authenticator` chain (`authenticators`, default
    `ansina.auth.authenticator.build_authenticators`) rather than a comparison against
    a single static config secret — issue #24's bootstrap token is generated once and
    never stored in config at all, so config-based comparison can't work for it. Issue
    #24 shipped this as one inline DB lookup; #25 formalizes it as this chain so a
    follow-up milestone's federated-login authenticator is an append, not a rewrite.

    Issue #26: once a `Principal` is resolved, a request carrying an `X-Sudo-Token`
    header is additionally checked against `sudo` (`ansina.auth.sudo.SudoService`) and
    elevated via `Principal.with_sudo()` on a live match. `sudo=None` (the default)
    skips this step entirely rather than raising — `create_app` always supplies a real
    one; `sudo` stays optional here only so a test wiring this middleware directly
    (see `test_middleware_authenticators_param_is_real_dependency_injection`) isn't
    forced to build one just to exercise the `authenticators` chain. An absent,
    expired, revoked, or wrong sudo token deliberately never turns a request into a
    401 by itself — it just fails to elevate, so a non-sensitive route is unaffected
    and a sensitive one still answers its own 403 `CODE_SUDO_REQUIRED`, not a
    misleading "your bearer token is bad."
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        enabled: bool,
        db: Database,
        authenticators: tuple[Authenticator, ...] | None = None,
        sudo: SudoService | None = None,
    ) -> None:
        self._app = app
        self._enabled = enabled
        self._db = db
        self._authenticators = (
            authenticators if authenticators is not None else build_authenticators(db)
        )
        self._sudo = sudo

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or not self._enabled
            or scope["path"] in PUBLIC_PATHS
        ):
            await self._app(scope, receive, send)
            return

        provided = _extract_bearer_token(scope)
        principal = None
        if provided is not None:
            # Blocking sqlite I/O plus a per-row salted-hash comparison — offloaded to
            # a worker thread so it never blocks the event loop for other in-flight
            # requests. `Database` already hands out one connection per thread by
            # design (see `storage/database.py`), so a worker thread reused across
            # requests keeps and reuses its own connection rather than reopening one.
            principal = await anyio.to_thread.run_sync(
                resolve_principal, self._db, self._authenticators, provided
            )

        if principal is not None and self._sudo is not None:
            sudo_token = _extract_sudo_token(scope)
            if sudo_token is not None:
                grant = await anyio.to_thread.run_sync(
                    self._sudo.resolve, principal.user.id, sudo_token
                )
                if grant is not None:
                    principal = principal.with_sudo(grant.id)

        if principal is None:
            # Missing header, malformed header, an unrecognized token, and an inactive
            # user's token all take this one path — no oracle that would let a caller
            # distinguish any of those from response shape or content.
            response = problem_response(
                status=401,
                code=CODE_UNAUTHORIZED,
                title="Unauthorized",
                detail="A valid bearer token is required.",
                headers={"WWW-Authenticate": _WWW_AUTHENTICATE},
            )
            await response(scope, receive, send)
            return

        scope.setdefault("state", {})["principal"] = principal
        await self._app(scope, receive, send)
