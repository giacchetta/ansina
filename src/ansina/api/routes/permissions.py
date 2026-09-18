"""`GET /auth/permissions` — the resource/verb catalog. See issues #27 and #38.

The `resources` table (populated by `ansina.api.route_audit.audit_route_coverage`'s own
walk, issue #25) is this route's entire source. Issue #38 makes it a trustworthy input
for a future "let an Admin define a custom role" UI (#40) in three ways: each entry
lists the verbs its resource is *actually served on* (not `list(Verb)` unconditionally
— a route's own declared methods, unioned across every route sharing that resource),
each carries its `auth.policy.PolicyClass` (ordinary / `auth.*` / `me.*`), and every
`me.*` resource is marked non-grantable — every builtin role already holds every verb
there, so offering e.g. `me.tokens:DELETE` as a grantable row would communicate an
escalation that doesn't exist.
"""

from __future__ import annotations

import anyio.to_thread
from fastapi import APIRouter, Request
from fastapi.params import Depends
from pydantic import BaseModel

from ansina.api.authorization import require
from ansina.auth.models import Verb, ordered_verbs
from ansina.auth.policy import PolicyClass, is_grantable, policy_class
from ansina.auth.repositories import ResourceRepository
from ansina.storage.database import Database

router = APIRouter(prefix="/auth/permissions")

_RESOURCE = "auth.permissions"
_DESCRIPTION = (
    "The catalogued resources with the verbs each is actually served on, its policy "
    "class, and whether it's grantable — the discovery surface a custom-role editor "
    "builds on."
)


def _require_read() -> Depends:
    return Depends(require(_RESOURCE, description=_DESCRIPTION))


class ResourcePermissionsOut(BaseModel):
    resource: str
    description: str
    verbs: list[Verb]
    policy_class: PolicyClass
    grantable: bool


def _list_permissions(db: Database) -> list[ResourcePermissionsOut]:
    return [
        ResourcePermissionsOut(
            resource=resource.name,
            description=resource.description,
            verbs=list(ordered_verbs(resource.verbs)),
            policy_class=policy_class(resource.name),
            grantable=is_grantable(resource.name),
        )
        for resource in ResourceRepository(db).list_all()
    ]


@router.get(
    "", response_model=list[ResourcePermissionsOut], dependencies=[_require_read()]
)
async def list_permissions(request: Request) -> list[ResourcePermissionsOut]:
    return await anyio.to_thread.run_sync(_list_permissions, request.app.state.db)
