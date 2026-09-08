"""Shared request-identity helpers — reading the `Principal` `BearerAuthMiddleware`
already resolved onto `request.state`, and the one 401 shape a route reports when
there isn't one. See issue #30.

Originally two private copies (`routes/sudo.py`'s `_principal`/`_no_identity_response`,
`routes/role_assignments.py`'s own `_principal`, whose docstring said it "mirrors
routes/sudo.py's own helper") — lifted here once a third caller (`routes/me.py`) needed
the same thing, since a follow-up issue (#28) adds more `me.*` routes that will too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request
from fastapi.responses import JSONResponse

from ansina.api.problems import CODE_UNAUTHORIZED, problem_response

if TYPE_CHECKING:
    from ansina.auth.principal import Principal

NO_IDENTITY_DETAIL = (
    "no resolved identity to act as — security.enabled = false disables the identity "
    "model entirely"
)


def current_principal(request: Request) -> Principal | None:
    """`request.state.principal` if one was resolved, else `None` — `security.
    enabled = false` never sets it at all (`Starlette`'s `State` raises
    `AttributeError` on a missing attribute, so this can't be a bare access).
    """
    return getattr(request.state, "principal", None)


def no_identity_response() -> JSONResponse:
    """The 401 `problem+json` a route reports when `current_principal()` came back
    `None` — there's no "who" to act as.
    """
    return problem_response(
        status=401,
        code=CODE_UNAUTHORIZED,
        title="Unauthorized",
        detail=NO_IDENTITY_DETAIL,
    )
