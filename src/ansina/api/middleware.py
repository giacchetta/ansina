"""Request-id assignment — feeds the correlation-id contextvar from issue #3.

Pure ASGI middleware (`__call__(scope, receive, send)`), not
`starlette.BaseHTTPMiddleware`: that class buffers the response and runs the downstream
handler in a separate task, which is exactly what breaks nesting
`contextvars.ContextVar.set()`/`.reset()` cleanly around streaming responses. Plain ASGI
has no such pitfall — the request/response cycle runs as one coroutine.

Also emits one structured access-log line per request, inside the id's scope — this is
what makes the correlation id actually correlate anything: without at least one log line
per request carrying it, `request_id_scope()` would bind a value nothing ever surfaces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ansina.logging import get_logger
from ansina.logging.context import request_id_scope

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = get_logger(__name__)

_HEADER_NAME = b"x-request-id"
# Printable ASCII only, and bounded — an inbound header is untrusted input that ends up
# in every log line for the request, so it must not be able to inject control characters
# or grow a log line without bound.
_MAX_LEN = 128


def _extract_inbound_id(scope: Scope) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == _HEADER_NAME:
            try:
                decoded: str = value.decode("ascii")
            except UnicodeDecodeError:
                return None
            if 0 < len(decoded) <= _MAX_LEN and decoded.isprintable():
                return decoded
            return None
    return None


class RequestIdMiddleware:
    """Binds a request id (inbound `X-Request-ID`, or a fresh one) for the request's
    lifetime, and echoes it back on the response.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        inbound_id = _extract_inbound_id(scope)
        with request_id_scope(inbound_id) as request_id:
            status_code = 0

            async def send_with_header(message: Message) -> None:
                nonlocal status_code
                if message["type"] == "http.response.start":
                    status_code = message["status"]
                    headers = message.setdefault("headers", [])
                    headers.append((_HEADER_NAME, request_id.encode("ascii")))
                await send(message)

            await self._app(scope, receive, send_with_header)
            logger.info(
                "request completed",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status_code": status_code,
                },
            )
