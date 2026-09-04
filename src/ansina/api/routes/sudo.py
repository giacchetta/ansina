"""Sudo step-up endpoints. See issue #26.

`POST /auth/sudo` re-verifies the caller's identity through whatever `StepUpVerifier`
`ansina.auth.step_up.StepUpRegistry` resolves for them (M2: password) and, on success,
issues a short-lived grant the caller then presents on sensitive `auth.*` calls via
`X-Sudo-Token` (`ansina.api.auth.BearerAuthMiddleware` reads it). `DELETE /auth/sudo`
lets the caller step back down deliberately; `DELETE /auth/sudo/grants` is the
break-glass path — it revokes *every* user's active grant, itself gated
`sensitive=True` so reaching it already requires a live grant.

All three are `auth.*` resources, so `auth.policy.permitted_verbs` already restricts
them to Maintain/Admin with no policy change needed — `Read`/`Write` get the ordinary
403 `ansina.forbidden`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio.to_thread
from fastapi import APIRouter, Request
from fastapi.params import Depends
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from ansina.api.authorization import require
from ansina.api.problems import CODE_UNAUTHORIZED, problem_response

if TYPE_CHECKING:
    from ansina.auth.principal import Principal
    from ansina.auth.sudo import SudoService

router = APIRouter(prefix="/auth")

_SUDO_RESOURCE = "auth.sudo"
_SUDO_DESCRIPTION = (
    "POST /auth/sudo (step up) and DELETE /auth/sudo (revoke your own grant)."
)
_GRANTS_RESOURCE = "auth.sudo.grants"
_GRANTS_DESCRIPTION = (
    "DELETE /auth/sudo/grants — break-glass: revokes every active sudo grant."
)

_NO_IDENTITY_DETAIL = (
    "no resolved identity to step up as — security.enabled = false disables the "
    "identity model entirely"
)


class SudoRequest(BaseModel):
    """Opaque payload handed to the resolved `StepUpVerifier` unexamined — the *port*
    decides what a payload must contain (a `password` key, for M2's one verifier),
    never this route.
    """

    model_config = ConfigDict(extra="allow")


class SudoGrantResponse(BaseModel):
    token: str
    expires_at: str
    verifier: str


def _require_sudo() -> Depends:
    """Not `sensitive=True`: requiring a live sudo grant to *obtain* one is
    unsatisfiable, and a caller must always be able to revoke their own grant. See
    `require()`'s own construction convention in `routes/heart.py`.
    """
    return Depends(require(_SUDO_RESOURCE, description=_SUDO_DESCRIPTION))


def _require_sudo_grants() -> Depends:
    return Depends(
        require(_GRANTS_RESOURCE, description=_GRANTS_DESCRIPTION, sensitive=True)
    )


def _principal(request: Request) -> Principal | None:
    """`request.state.principal` if one was resolved, else `None` — `security.
    enabled = false` never sets it at all (`Starlette`'s `State` raises
    `AttributeError` on a missing attribute, so this can't be a bare access).
    """
    return getattr(request.state, "principal", None)


def _no_identity_response() -> JSONResponse:
    return problem_response(
        status=401,
        code=CODE_UNAUTHORIZED,
        title="Unauthorized",
        detail=_NO_IDENTITY_DETAIL,
    )


@router.post(
    "/sudo",
    response_model=None,
    responses={
        200: {"model": SudoGrantResponse},
        401: {"description": "Step-up verification failed."},
        429: {"description": "Locked out after too many failed attempts."},
    },
    dependencies=[_require_sudo()],
)
async def step_up(request: Request, payload: SudoRequest) -> JSONResponse:
    principal = _principal(request)
    if principal is None:
        return _no_identity_response()

    sudo: SudoService = request.app.state.sudo
    # Argon2id verification is deliberately CPU-heavy (see `auth.hashing`'s module
    # docstring) — offloaded to a worker thread so it never blocks the event loop for
    # other in-flight requests, the same pattern `BearerAuthMiddleware` already uses.
    issued = await anyio.to_thread.run_sync(
        sudo.step_up, principal, payload.model_dump()
    )
    if issued is None:
        return problem_response(
            status=401,
            code=CODE_UNAUTHORIZED,
            title="Unauthorized",
            detail="Step-up verification failed.",
        )
    return JSONResponse(
        status_code=200,
        content={
            "token": issued.token,
            "expires_at": issued.expires_at,
            "verifier": issued.verifier,
        },
    )


@router.delete(
    "/sudo",
    status_code=204,
    dependencies=[_require_sudo()],
)
async def revoke_own_grant(request: Request) -> Response:
    principal = _principal(request)
    if principal is None:
        return _no_identity_response()

    sudo: SudoService = request.app.state.sudo
    await anyio.to_thread.run_sync(sudo.revoke_for_user, principal.user.id)
    return Response(status_code=204)


@router.delete(
    "/sudo/grants",
    status_code=204,
    dependencies=[_require_sudo_grants()],
)
async def revoke_all_grants(request: Request) -> Response:
    """Break-glass: revokes every user's active sudo grant, including the caller's
    own — that is the point of a break-glass endpoint, not an oversight.
    """
    sudo: SudoService = request.app.state.sudo
    await anyio.to_thread.run_sync(sudo.revoke_all)
    return Response(status_code=204)
