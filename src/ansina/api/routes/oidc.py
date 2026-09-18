"""`POST /auth/oidc/login` + `GET /auth/oidc/callback` — the OIDC login exchange. See
issue #43.

The first non-health-probe routes in `api/auth.py`'s `PUBLIC_PATHS`: a caller cannot be
authenticated *before* logging in, so both routes are deliberately reachable with no
bearer token — the same "deny-by-default, `PUBLIC_PATHS` is the only carve-out" rule
every other route follows is what makes this widening worth calling out explicitly
rather than leaving it to be discovered. Neither declares a `require(...)`
authorization dependency — `api.route_audit.audit_route_coverage` already skips every
path in `PUBLIC_PATHS`, so this contributes no `resources` catalog entry, exactly like
`/healthz`/`/readyz`.

The actual login-exchange logic (state validation, code exchange, ID-token validation,
provisioning, role-mapping refresh) lives in `ansina.auth.oidc_login.OidcLoginService`
— this module is thin: check `request.app.state.oidc` for `None` (503 when
`[security.oidc] enabled = false`, mirroring `routes/heart.py`'s `CODE_HEART_DISABLED`
shape), validate the callback's own query parameters, and mint the resulting
`api_token` via `ansina.api.tokens.issue_token` — see `oidc_login`'s module docstring
for why that mint happens here rather than inside the service itself (a circular-import
boundary, not a style preference).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio.to_thread
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ansina.api import tokens as tokens_api
from ansina.api.problems import CODE_OIDC_DISABLED, problem_response
from ansina.auth.oidc_login import OidcCallbackError

if TYPE_CHECKING:
    from ansina.auth.oidc_login import OidcLoginService
    from ansina.storage.database import Database

router = APIRouter(prefix="/auth/oidc")

# Visible in `GET /auth/me/tokens`/`GET /auth/users/{id}/tokens` so an operator can
# tell an OIDC-minted token apart from a manually-issued one.
_TOKEN_LABEL = "oidc login"

_DISABLED_DETAIL = (
    "OIDC federated login is disabled ([security.oidc] enabled = false) — there is "
    "no login exchange to start or complete"
)


class OidcLoginResponse(BaseModel):
    """`POST /auth/oidc/login`'s result: redirect the resource owner's browser to
    `authorization_url`. `state`/`expires_at` are informational — the callback itself
    round-trips `state` via the IdP's own redirect, never as something this response's
    caller resubmits directly.
    """

    authorization_url: str
    state: str
    expires_at: str


def _disabled_response() -> JSONResponse:
    return problem_response(
        status=503,
        code=CODE_OIDC_DISABLED,
        title="OIDC Disabled",
        detail=_DISABLED_DETAIL,
    )


def _get_oidc(request: Request) -> OidcLoginService | None:
    oidc: OidcLoginService | None = request.app.state.oidc
    return oidc


def _complete_login(
    oidc: OidcLoginService, db: Database, code: str, state: str
) -> tokens_api.IssuedTokenResponse:
    """Runs the login exchange end to end, then mints the resulting `api_token` —
    both synchronous, both in this one function, so the single
    `anyio.to_thread.run_sync` call below offloads the whole thing (state lookup,
    outbound HTTP to the IdP, ID-token validation, provisioning, role sync, and the
    token insert) in one worker-thread hop rather than two.
    """
    user = oidc.complete_login(code, state)
    return tokens_api.issue_token(
        db,
        user.id,
        _TOKEN_LABEL,
        ttl_seconds=oidc.token_ttl_seconds,
        clock=oidc.clock,
    )


@router.post(
    "/login",
    response_model=None,
    responses={
        200: {"model": OidcLoginResponse},
        503: {"description": "OIDC federated login is disabled."},
    },
)
async def oidc_login(request: Request) -> OidcLoginResponse | JSONResponse:
    oidc = _get_oidc(request)
    if oidc is None:
        return _disabled_response()
    start = await anyio.to_thread.run_sync(oidc.start_login)
    return OidcLoginResponse(
        authorization_url=start.authorization_url,
        state=start.state,
        expires_at=start.expires_at,
    )


@router.get(
    "/callback",
    response_model=None,
    responses={
        200: {"model": tokens_api.IssuedTokenResponse},
        400: {
            "description": "Malformed callback — missing code/state, or the "
            "identity provider reported an error."
        },
        401: {"description": "The id_token failed validation."},
        403: {"description": "Provisioning was refused for this identity."},
        502: {"description": "The identity provider was unreachable or misbehaved."},
        503: {"description": "OIDC federated login is disabled."},
    },
)
async def oidc_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> tokens_api.IssuedTokenResponse | JSONResponse:
    oidc = _get_oidc(request)
    if oidc is None:
        return _disabled_response()
    if error is not None:
        raise OidcCallbackError(
            f"the identity provider returned an error: {error}",
            details={"error": error},
        )
    if not code or not state:
        # `details` keys must never collide with `Problem`'s own reserved top-level
        # fields (`problem_response`'s `code=exc.code` in particular) — `code_present`/
        # `state_present`, not `code`/`state`.
        raise OidcCallbackError(
            "the callback is missing 'code' and/or 'state'",
            details={"code_present": bool(code), "state_present": bool(state)},
        )
    return await anyio.to_thread.run_sync(
        _complete_login, oidc, request.app.state.db, code, state
    )
