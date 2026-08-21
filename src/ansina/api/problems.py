"""RFC 9457 `application/problem+json` — the one error shape every route can produce.

`errors.py` is deliberately transport-free ("The API layer maps `code` to an HTTP
response"); this module is that mapping. Non-`AnsinaError` failures (404, 405, 422,
unhandled 500, 503 readiness) don't have an `AnsinaError` subclass to carry a `code` —
one is minted here instead, so every error response, regardless of which door produced
it, carries a stable `code` and the request's correlation id.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ansina.errors import AnsinaError, ConfigurationError
from ansina.logging import get_request_id

PROBLEM_MEDIA_TYPE = "application/problem+json"

# Codes for failures that don't originate from an `AnsinaError` subclass.
CODE_REQUEST_INVALID = "ansina.request.invalid"
CODE_NOT_FOUND = "ansina.not_found"
CODE_METHOD_NOT_ALLOWED = "ansina.method_not_allowed"
CODE_INTERNAL_ERROR = "ansina.internal_error"
CODE_NOT_READY = "ansina.not_ready"
CODE_UNAUTHORIZED = "ansina.unauthorized"

# `AnsinaError` subclass -> HTTP status. Looked up by walking the MRO, so a future
# subclass with no entry of its own inherits its nearest mapped ancestor's status
# rather than needing a new row here. `AnsinaError` maps to 500 as the fallback.
_STATUS_BY_ERROR_TYPE: dict[type[AnsinaError], int] = {
    AnsinaError: 500,
    ConfigurationError: 500,
}


def status_for_error(exc: AnsinaError) -> int:
    """The HTTP status for `exc`, from its own type or the nearest mapped ancestor."""
    for cls in type(exc).__mro__:
        if issubclass(cls, AnsinaError) and cls in _STATUS_BY_ERROR_TYPE:
            return _STATUS_BY_ERROR_TYPE[cls]
    return 500  # unreachable: AnsinaError itself is always in _STATUS_BY_ERROR_TYPE,
    # and it's always in every subclass's __mro__, so the loop above always returns
    # first. Kept only because mypy --strict requires a return on every path.


class Problem(BaseModel):
    """RFC 9457 members plus Ansina's extension members (`code`, `request_id`)."""

    model_config = ConfigDict(extra="allow")

    type: str
    title: str
    status: int
    detail: str
    code: str
    request_id: str | None = None


def problem_response(
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    extra: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Build a `problem+json` response.

    `request_id` is always read from context here — never accepted as a parameter, so a
    handler can't accidentally report the wrong one. `headers` is for response headers
    that aren't part of the problem body itself — e.g. `WWW-Authenticate` on a 401.
    """
    problem = Problem(
        type=f"urn:ansina:error:{code}",
        title=title,
        status=status,
        detail=detail,
        code=code,
        request_id=get_request_id(),
        **(dict(extra) if extra else {}),
    )
    return JSONResponse(
        status_code=status,
        content=problem.model_dump(exclude_none=True),
        media_type=PROBLEM_MEDIA_TYPE,
        headers=dict(headers) if headers else None,
    )
