"""Self-identity: `GET /auth/me`. See issue #30.

The resource declared here is `me.profile`, while the path is `/auth/me` — that is
consistent, not sloppy. A resource is a stable, dotted, **URL-independent** name
(`docs/architecture/blueprint.md` §Identity & access control); the policy has always
keyed on the resource, never the path.

`me.profile` is the first resource to fall under `auth.policy`'s `me.*` carve-out
(`is_self_resource`): every builtin role — `Read` included — holds every verb on it,
and it is never `sensitive=True`, so no sudo grant is ever required. This is not an
escalation: the subject of a `me.*` action is always the authenticated caller
themselves, resolved once by `BearerAuthMiddleware` onto `request.state.principal`, so
there is nothing this route reads that the caller doesn't already own. No database
read happens here at all — `resolve_principal` has already done that work.

`security.enabled = false` never resolves a `Principal` at all; handled the same way
`routes/sudo.py`/`routes/role_assignments.py` already do, via `api.identity`.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.params import Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ansina.api.authorization import require
from ansina.api.identity import current_principal, no_identity_response

router = APIRouter(prefix="/auth")

_RESOURCE = "me.profile"
_DESCRIPTION = "The authenticated caller's own identity: user, roles, sudo status."


def _require_read() -> Depends:
    return Depends(require(_RESOURCE, description=_DESCRIPTION))


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
