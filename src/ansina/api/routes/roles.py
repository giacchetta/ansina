"""`GET /auth/roles` — the read-only role catalog. See issue #27.

Builtin roles are read-only over the API, including for `Admin` — their grants are
owned by `auth.reconciler.reconcile_builtin_roles`, not editable via this surface, in
M2 or after. No create/update/delete route exists here: that omission (rather than a
stub that 403s) is deliberate, per the issue's own framing — a follow-up custom-roles
milestone adds write routes over these same tables, this one only ships discovery.
"""

from __future__ import annotations

import anyio.to_thread
from fastapi import APIRouter, Request
from fastapi.params import Depends
from pydantic import BaseModel

from ansina.api.authorization import require
from ansina.auth.models import Role, Verb
from ansina.auth.repositories import RolePermissionRepository, RoleRepository
from ansina.storage.database import Database

router = APIRouter(prefix="/auth/roles")

_RESOURCE = "auth.roles"
_DESCRIPTION = (
    "The role catalog (builtin, and later custom) and each role's current grants — "
    "read-only; no create/update/delete route exists."
)


def _require_read() -> Depends:
    return Depends(require(_RESOURCE, description=_DESCRIPTION))


class GrantOut(BaseModel):
    resource: str
    verb: Verb


class RoleOut(BaseModel):
    id: str
    slug: str
    name: str
    description: str
    builtin: bool
    created_at: str
    permissions: list[GrantOut]

    @classmethod
    def from_model(cls, role: Role, grants: list[GrantOut]) -> RoleOut:
        return cls(
            id=role.id,
            slug=role.slug,
            name=role.name,
            description=role.description,
            builtin=role.builtin,
            created_at=role.created_at,
            permissions=grants,
        )


def _list_roles(db: Database) -> list[RoleOut]:
    permissions = RolePermissionRepository(db)
    roles = []
    for role in RoleRepository(db).list_all():
        grants = [
            GrantOut(resource=p.resource, verb=p.verb)
            for p in permissions.list_for_role(role.id)
        ]
        roles.append(RoleOut.from_model(role, grants))
    return roles


@router.get("", response_model=list[RoleOut], dependencies=[_require_read()])
async def list_roles(request: Request) -> list[RoleOut]:
    return await anyio.to_thread.run_sync(_list_roles, request.app.state.db)
