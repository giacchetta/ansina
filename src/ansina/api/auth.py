"""Static bearer-token enforcement — the internal API boundary from issue #5.

Pure ASGI middleware (`__call__(scope, receive, send)`), not
`starlette.BaseHTTPMiddleware`, for the same reason `RequestIdMiddleware` is: that class
buffers the response and runs the downstream handler in a separate task, which breaks
nesting `contextvars.ContextVar.set()`/`.reset()` cleanly around streaming responses.

Deny-by-default: every route requires the token except `PUBLIC_PATHS`. A route added by
a later milestone is protected automatically, with no per-route opt-in to forget.

Builds the `problem+json` response itself rather than raising — the exception handlers
registered on the `FastAPI` app sit *inside* the middleware stack, so a raised exception
here would escape past them and reach the client as a bare, framework-default 500.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

from ansina.api.problems import CODE_UNAUTHORIZED, problem_response

if TYPE_CHECKING:
    from pydantic import SecretStr
    from starlette.types import ASGIApp, Receive, Scope, Send

# Reachable with no token, even when auth is enabled. Everything else is
# deny-by-default, including /version and the OpenAPI/docs routes.
PUBLIC_PATHS = frozenset({"/healthz", "/readyz"})

_HEADER_NAME = b"authorization"
_SCHEME = "bearer"
_WWW_AUTHENTICATE = "Bearer"


def verify_token(provided: str, expected: str) -> bool:
    """Constant-time comparison — never `==` on secret material.

    No length or prefix short-circuit before the call: `hmac.compare_digest` is
    handed the raw candidate as-is, so nothing about the caller's input can be timed
    to leak how much of the real token it got right.
    """
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


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
    """Rejects any non-`PUBLIC_PATHS` request that lacks a valid bearer token.

    `token=None` means auth is disabled (dev mode) — every request passes through.
    `config.settings.Settings` refuses to construct at all when that's paired with a
    non-loopback bind, so a token-less app is never reachable off the local machine.
    """

    def __init__(self, app: ASGIApp, *, token: SecretStr | None) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or self._token is None
            or scope["path"] in PUBLIC_PATHS
        ):
            await self._app(scope, receive, send)
            return

        provided = _extract_bearer_token(scope)
        if provided is None or not verify_token(
            provided, self._token.get_secret_value()
        ):
            # Missing header, malformed header, and wrong token all take this one path —
            # no oracle that would let a caller distinguish "no token sent" from "wrong
            # token sent" from response shape or content.
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
