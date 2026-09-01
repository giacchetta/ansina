"""Bearer-token enforcement — the internal API boundary from issue #5, extended by
issue #24 to check the RBAC identity model's `credentials` table instead of a single
static config secret.

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
from ansina.auth.repositories import CredentialRepository

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from ansina.storage.database import Database

# Reachable with no token, even when auth is enabled. Everything else is
# deny-by-default, including /version and the OpenAPI/docs routes.
PUBLIC_PATHS = frozenset({"/healthz", "/readyz"})

_HEADER_NAME = b"authorization"
_SCHEME = "bearer"
_WWW_AUTHENTICATE = "Bearer"


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


class BearerAuthMiddleware:
    """Rejects any non-`PUBLIC_PATHS` request that doesn't carry a currently-active
    API-token credential.

    `enabled=False` (`Settings.security.enabled`) disables authentication outright —
    every request passes through. `config.settings.Settings` refuses to construct at
    all when that's paired with a non-loopback bind (`Settings._refuse_unsafe_bind`),
    so a disabled-auth app is never reachable off the local machine.

    Verification is DB-backed (`ansina.auth.repositories.CredentialRepository
    .find_user_by_api_token`) rather than a comparison against a single static config
    secret — issue #24's bootstrap token is generated once and never stored in config
    at all, so config-based comparison can't work for it. This is a minimal slice of
    issue #25's planned `Authenticator` chain pulled forward; #25 builds the richer
    `Principal`/roles-aware `require()` dependency on top of this same lookup — this
    middleware only ever answers "is there *any* identity this token belongs to,"
    never "what can it do."
    """

    def __init__(self, app: ASGIApp, *, enabled: bool, db: Database) -> None:
        self._app = app
        self._enabled = enabled
        self._db = db

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or not self._enabled
            or scope["path"] in PUBLIC_PATHS
        ):
            await self._app(scope, receive, send)
            return

        provided = _extract_bearer_token(scope)
        user = None
        if provided is not None:
            # Blocking sqlite I/O plus a per-row salted-hash comparison — offloaded to
            # a worker thread so it never blocks the event loop for other in-flight
            # requests. `Database` already hands out one connection per thread by
            # design (see `storage/database.py`), so a worker thread reused across
            # requests keeps and reuses its own connection rather than reopening one.
            credentials = CredentialRepository(self._db)
            user = await anyio.to_thread.run_sync(
                credentials.find_user_by_api_token, provided
            )

        if user is None:
            # Missing header, malformed header, and an unrecognized token all take this
            # one path — no oracle that would let a caller distinguish "no token sent"
            # from "wrong token sent" from response shape or content.
            response = problem_response(
                status=401,
                code=CODE_UNAUTHORIZED,
                title="Unauthorized",
                detail="A valid bearer token is required.",
                headers={"WWW-Authenticate": _WWW_AUTHENTICATE},
            )
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)
