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

from ansina.auth.authorization import ForbiddenError, SudoRequiredError
from ansina.auth.encryption import EncryptionKeyMissingError
from ansina.auth.management import (
    BootstrapIdentityError,
    InvalidGrantError,
    LastAdminError,
    NotFoundError,
    SelfEscalationError,
    TokenAlreadyIssuedError,
    TotpAlreadyEnrolledError,
)
from ansina.auth.repositories import (
    BuiltinRoleError,
    DuplicateError,
    RoleInUseError,
    UnknownSubjectError,
)
from ansina.auth.sudo import StepUpUnavailableError, SudoLockedOutError
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
CODE_HEART_DISABLED = "ansina.heart.disabled"
CODE_FORBIDDEN = ForbiddenError.code
CODE_SUDO_REQUIRED = SudoRequiredError.code
CODE_SUDO_LOCKED_OUT = SudoLockedOutError.code
CODE_STEP_UP_UNAVAILABLE = StepUpUnavailableError.code
CODE_SELF_ESCALATION = SelfEscalationError.code
CODE_LAST_ADMIN = LastAdminError.code
CODE_NOT_FOUND_AUTH = NotFoundError.code
CODE_DUPLICATE = DuplicateError.code
CODE_UNKNOWN_SUBJECT = UnknownSubjectError.code
CODE_BOOTSTRAP_IDENTITY = BootstrapIdentityError.code
CODE_TOKEN_ALREADY_ISSUED = TokenAlreadyIssuedError.code
CODE_BUILTIN_ROLE_IMMUTABLE = BuiltinRoleError.code
CODE_ROLE_IN_USE = RoleInUseError.code
CODE_INVALID_GRANT = InvalidGrantError.code
CODE_TOTP_ALREADY_ENROLLED = TotpAlreadyEnrolledError.code
CODE_ENCRYPTION_KEY_MISSING = EncryptionKeyMissingError.code

# `AnsinaError` subclass -> HTTP status. Looked up by walking the MRO, so a future
# subclass with no entry of its own inherits its nearest mapped ancestor's status
# rather than needing a new row here. `AnsinaError` maps to 500 as the fallback.
_STATUS_BY_ERROR_TYPE: dict[type[AnsinaError], int] = {
    AnsinaError: 500,
    ConfigurationError: 500,
    # 403s, distinguishable from CODE_UNAUTHORIZED (401, no/invalid identity) by
    # `code` alone — neither leaks which credential component was wrong.
    ForbiddenError: 403,
    SudoRequiredError: 403,
    # 403, same family as the two above — the caller is authenticated but holds no
    # step-up factor that could ever satisfy this request (issue #37).
    StepUpUnavailableError: 403,
    # 429, not 401/403: the caller of POST /auth/sudo is already authenticated, so
    # disclosing "you're locked out" leaks nothing a wrong-password 401 wouldn't
    # already suggest, and it's a rate-limiting concern, not an identity/permission
    # one (issue #26).
    SudoLockedOutError: 429,
    # 503: `POST /auth/me/totp` (issue #41) tried to encrypt a fresh secret with no
    # `[security.encryption] key` configured — a server misconfiguration, never a
    # client mistake, and the "feature isn't available right now" family
    # `CODE_HEART_DISABLED` already uses for the same shape of problem.
    EncryptionKeyMissingError: 503,
    # 403, same family as ForbiddenError — the caller is otherwise entitled to mutate
    # this resource, but not to hand out a grant it doesn't itself hold (issue #27).
    SelfEscalationError: 403,
    # 403, same family again — the caller is otherwise entitled to call this token
    # route, but the bootstrap identity's one credential is off-limits to it
    # (issue #28's invariant A).
    BootstrapIdentityError: 403,
    # 409: the request is well-formed and the caller is otherwise authorized, but
    # applying it would leave the RBAC model in a state with no recovery path.
    LastAdminError: 409,
    DuplicateError: 409,
    # 409, same family — the target already holds an api_token; issue #28's
    # invariant B makes this route first-credential-only.
    TokenAlreadyIssuedError: 409,
    # 409, same family again — the caller already holds a live TOTP enrollment
    # (issue #41); `DELETE /auth/me/totp` (or an Admin's `.../totp` reset) first.
    TotpAlreadyEnrolledError: 409,
    # 409, same family again — the request is well-formed and the caller is
    # otherwise authorized, but the target role is builtin (`PATCH`/`DELETE
    # /auth/roles/{id}`, issue #40) or still referenced by a `role_assignments` row
    # (`DELETE`, issue #40's repository-layer in-use guard).
    BuiltinRoleError: 409,
    RoleInUseError: 409,
    # 404: a path referenced a user/group/role id, or a role_assignments subject, that
    # doesn't exist.
    NotFoundError: 404,
    UnknownSubjectError: 404,
    # 422: the request is syntactically valid JSON but semantically invalid — a
    # submitted grant names an uncatalogued, non-grantable, or unserved (resource,
    # verb) pair (issue #40), alongside FastAPI's own validation-error 422s.
    InvalidGrantError: 422,
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
