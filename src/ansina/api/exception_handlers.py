"""Every error door FastAPI can open, rendered through the one `problem_response` shape.

Registered by `create_app`: `AnsinaError` subclasses, Starlette's `HTTPException`
(404, 405, and any route-raised `HTTPException`), FastAPI's `RequestValidationError`
(422), and a catch-all `Exception` (unhandled bugs -> 500). Without the catch-all, an
unhandled exception would reach the client as a bare, framework-default 500 instead of
`problem+json`.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from ansina.api.problems import (
    CODE_INTERNAL_ERROR,
    CODE_METHOD_NOT_ALLOWED,
    CODE_NOT_FOUND,
    CODE_REQUEST_INVALID,
    problem_response,
    status_for_error,
)
from ansina.errors import AnsinaError
from ansina.logging import get_logger

logger = get_logger(__name__)

# HTTP status -> (code, title) for the Starlette `HTTPException` statuses that don't
# already carry their own message worth preserving as `detail`. Anything not listed
# falls back to a generic `ansina.http_error` code with the status's own reason phrase.
_STARLETTE_CODES: dict[int, str] = {
    404: CODE_NOT_FOUND,
    405: CODE_METHOD_NOT_ALLOWED,
}


async def ansina_error_handler(_request: Request, exc: Exception) -> Response:
    """`exc` is always an `AnsinaError` — FastAPI's handler signature is
    `Exception`-typed, but registration below binds this only to `AnsinaError` and its
    subclasses.
    """
    assert isinstance(exc, AnsinaError)  # narrows for the branches below; see docstring
    headers = None
    retry_after = exc.details.get("retry_after_seconds")
    if isinstance(retry_after, int | float):
        # `SudoLockedOutError` (issue #26) is the first `AnsinaError` shaped as a
        # rate limit — a real `Retry-After` header alongside the body's own copy of
        # the same figure, for any client that honors it without parsing JSON.
        headers = {"Retry-After": str(int(retry_after))}
    return problem_response(
        status=status_for_error(exc),
        code=exc.code,
        title=type(exc).__name__,
        detail=str(exc),
        extra=exc.details or None,
        headers=headers,
    )


async def http_exception_handler(_request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)  # registration below guarantees this
    code = _STARLETTE_CODES.get(exc.status_code, "ansina.http_error")
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return problem_response(
        status=exc.status_code,
        code=code,
        title=detail,
        detail=detail,
    )


async def validation_error_handler(_request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)  # registration below guarantees this
    # `input` is deliberately dropped — FastAPI's default handler echoes the submitted
    # value back, which would reflect a caller's secret into the response body.
    errors = [
        {"loc": [str(part) for part in error["loc"]], "msg": error["msg"]}
        for error in exc.errors()
    ]
    return problem_response(
        status=422,
        code=CODE_REQUEST_INVALID,
        title="Request validation failed",
        detail="The request did not match the expected schema.",
        extra={"errors": errors},
    )


async def unhandled_exception_handler(_request: Request, exc: Exception) -> Response:
    logger.exception("Unhandled exception", exc_info=exc)
    return problem_response(
        status=500,
        code=CODE_INTERNAL_ERROR,
        title="Internal Server Error",
        detail="An unexpected error occurred.",
    )
