"""`GET /auth/permissions` — the resource/verb catalog. See issue #27.

The `resources` table (populated by `ansina.api.route_audit.audit_route_coverage`'s own
walk, issue #25) joined against every `Verb` — the full space of `(resource, verb)`
pairs a role *could* be granted, not what's currently granted (that's `GET /auth/
roles`). This is the discovery endpoint a future "let an Admin define a custom role" UI
needs to enumerate what it can grant; shipping it now means that follow-up milestone
adds no new discovery mechanism, only write routes alongside these read ones.
"""

from __future__ import annotations

import anyio.to_thread
from fastapi import APIRouter, Request
from fastapi.params import Depends
from pydantic import BaseModel

from ansina.api.authorization import require
from ansina.auth.models import Verb
from ansina.auth.repositories import ResourceRepository
from ansina.storage.database import Database

router = APIRouter(prefix="/auth/permissions")

_RESOURCE = "auth.permissions"
_DESCRIPTION = (
    "The catalogued resources crossed with every verb — the full space of grantable "
    "permissions, independent of which role currently holds what."
)


def _require_read() -> Depends:
    return Depends(require(_RESOURCE, description=_DESCRIPTION))


class ResourcePermissionsOut(BaseModel):
    resource: str
    description: str
    verbs: list[Verb]


_ALL_VERBS: list[Verb] = list(Verb)


def _list_permissions(db: Database) -> list[ResourcePermissionsOut]:
    return [
        ResourcePermissionsOut(
            resource=resource.name, description=resource.description, verbs=_ALL_VERBS
        )
        for resource in ResourceRepository(db).list_all()
    ]


@router.get(
    "", response_model=list[ResourcePermissionsOut], dependencies=[_require_read()]
)
async def list_permissions(request: Request) -> list[ResourcePermissionsOut]:
    return await anyio.to_thread.run_sync(_list_permissions, request.app.state.db)
