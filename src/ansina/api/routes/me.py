"""Self-identity: `GET /auth/me` (issue #30) plus self-service tokens (issue #28).

The resources declared here are `me.profile` and `me.tokens`, while the paths are
`/auth/me` and `/auth/me/tokens` — that is consistent, not sloppy. A resource is a
stable, dotted, **URL-independent** name (`docs/architecture/blueprint.md` §Identity &
access control); the policy has always keyed on the resource, never the path.

Both fall under `auth.policy`'s `me.*` carve-out (`is_self_resource`): every builtin
role — `Read` included — holds every verb on either, and neither is ever
`sensitive=True`, so no sudo grant is ever required. This is not an escalation: the
subject of a `me.*` action is always the authenticated caller themselves, resolved
once by `BearerAuthMiddleware` onto `request.state.principal`, so no route here can
reach another user's data. `GET /auth/me` reads nothing else at all —
`resolve_principal` has already done that work — but the token routes do touch the
database (`ansina.api.tokens`, shared with `api.routes.users`'s admin-on-behalf-of
surface), through `anyio.to_thread.run_sync` like every other DB-touching route.

One caller is refused on the token routes regardless of role: the synthetic bootstrap
identity, which holds exactly one `api_token`, ever — `POST`/`DELETE` 403
(`ansina.auth.bootstrap_identity`); see `ansina.auth.bootstrap`'s module docstring for
why. `GET` is unaffected — the bootstrap identity can still see its one token's
metadata.

`security.enabled = false` never resolves a `Principal` at all; handled the same way
`routes/sudo.py`/`routes/role_assignments.py` already do, via `api.identity`.
"""

from __future__ import annotations

import anyio.to_thread
from fastapi import APIRouter, Request
from fastapi.params import Depends
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from ansina.api import tokens as tokens_api
from ansina.api.authorization import require
from ansina.api.identity import current_principal, no_identity_response

router = APIRouter(prefix="/auth")

_RESOURCE = "me.profile"
_DESCRIPTION = "The authenticated caller's own identity: user, roles, sudo status."

_TOKENS_RESOURCE = "me.tokens"
_TOKENS_DESCRIPTION = (
    "The authenticated caller's own API tokens: mint, list, and revoke."
)


def _require_read() -> Depends:
    return Depends(require(_RESOURCE, description=_DESCRIPTION))


def _require_tokens() -> Depends:
    return Depends(require(_TOKENS_RESOURCE, description=_TOKENS_DESCRIPTION))


class MeOut(BaseModel):
    user_id: str
    username: str
    display_name: str
    roles: list[str]
    auth_method: str
    sudo_active: bool


@router.get(
    "/me",
    response_model=None,
    responses={
        200: {"model": MeOut},
        401: {
            "description": "No resolved identity (missing/invalid token, or dev "
            "mode with security disabled)."
        },
    },
    dependencies=[_require_read()],
)
async def get_me(request: Request) -> JSONResponse:
    principal = current_principal(request)
    if principal is None:
        return no_identity_response()

    body = MeOut(
        user_id=principal.user.id,
        username=principal.user.username,
        display_name=principal.user.display_name,
        roles=sorted(principal.role_slugs),
        auth_method=principal.auth_method.value,
        sudo_active=principal.sudo_active,
    )
    return JSONResponse(status_code=200, content=body.model_dump())


@router.post(
    "/me/tokens",
    response_model=None,
    responses={
        201: {"model": tokens_api.IssuedTokenResponse},
        401: {"description": "No resolved identity."},
        403: {
            "description": "The bootstrap identity — see the module docstring for why."
        },
    },
    status_code=201,
    dependencies=[_require_tokens()],
)
async def mint_own_token(
    request: Request, payload: tokens_api.IssueTokenRequest
) -> JSONResponse:
    principal = current_principal(request)
    if principal is None:
        return no_identity_response()

    issued = await anyio.to_thread.run_sync(
        tokens_api.issue_token, request.app.state.db, principal.user.id, payload.label
    )
    return JSONResponse(status_code=201, content=issued.model_dump())


@router.get(
    "/me/tokens",
    response_model=None,
    responses={
        200: {"model": list[tokens_api.TokenOut]},
        401: {"description": "No resolved identity."},
    },
    dependencies=[_require_tokens()],
)
async def list_own_tokens(request: Request) -> JSONResponse:
    principal = current_principal(request)
    if principal is None:
        return no_identity_response()

    listed = await anyio.to_thread.run_sync(
        tokens_api.list_tokens, request.app.state.db, principal.user.id
    )
    return JSONResponse(
        status_code=200, content=[token.model_dump() for token in listed]
    )


@router.delete(
    "/me/tokens/{token_id}",
    response_model=None,
    responses={
        204: {"description": "Revoked."},
        401: {"description": "No resolved identity."},
        403: {
            "description": "The bootstrap identity — see the module docstring for why."
        },
        404: {"description": "No such token id for the caller's own account."},
    },
    dependencies=[_require_tokens()],
)
async def revoke_own_token(request: Request, token_id: str) -> Response:
    principal = current_principal(request)
    if principal is None:
        return no_identity_response()

    await anyio.to_thread.run_sync(
        tokens_api.revoke_token, request.app.state.db, principal.user.id, token_id
    )
    return Response(status_code=204)
