"""`GET`/`POST`/`PATCH`/`DELETE /auth/roles` — the role catalog. See issues #27, #40.

Builtin roles stay read-only over the API, including for `Admin` — their grants are
owned by `auth.reconciler.reconcile_builtin_roles`, not editable via this surface, in
M2 or after (`PATCH`/`DELETE` refuse a `builtin=1` role regardless of caller). Issue #40
opens the write half of this surface: an `Admin` (or sudo'd `Maintain`, now that #37
makes the sudo gate fail-closed) may create a non-builtin role with a grant set drawn
from `GET /auth/permissions`'s catalog (#38), replace that grant set, or delete the role
— refused while it's still assigned to a user or a group.

One resource, `auth.roles`, split by verb like `routes/users.py`/`groups.py`: `GET` is
`sensitive=False`, every mutation is `sensitive=True` — the whole sudo gate, via #37's
fail-closed `authorize()`. Every mutation additionally runs the submitted grant set
through `auth.management.assert_grants_grantable` (is each grant even a real, grantable
verb on a catalogued resource?) *ahead of*
`auth.management.assert_may_grant_permissions` (does the caller itself hold what it's
trying to hand out?) — an uncatalogued resource can never be an escalation, so checking
catalog validity first means a typo'd resource name is never mistaken for one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio.to_thread
from fastapi import APIRouter, Request
from fastapi.params import Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ansina.api.authorization import require
from ansina.api.identity import current_principal
from ansina.auth.management import (
    NotFoundError,
    assert_grants_grantable,
    assert_may_grant_permissions,
)
from ansina.auth.models import Role, Verb
from ansina.auth.repositories import (
    BuiltinRoleError,
    RolePermissionRepository,
    RoleRepository,
)

if TYPE_CHECKING:
    from ansina.auth.principal import Principal
    from ansina.storage.database import Database

router = APIRouter(prefix="/auth/roles")

_RESOURCE = "auth.roles"
_DESCRIPTION = (
    "The role catalog (builtin, and custom) and each role's current grants. "
    "Builtin roles are read-only; a custom role can be created, have its grant set "
    "replaced, or be deleted (refused while still assigned)."
)


def _require_read() -> Depends:
    return Depends(require(_RESOURCE, description=_DESCRIPTION))


def _require_write() -> Depends:
    return Depends(require(_RESOURCE, description=_DESCRIPTION, sensitive=True))


class GrantIn(BaseModel):
    resource: str
    verb: Verb


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


class CreateRoleRequest(BaseModel):
    slug: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    permissions: list[GrantIn] = Field(default_factory=list)


class UpdateRoleRequest(BaseModel):
    permissions: list[GrantIn]


def _to_grant_set(grants: list[GrantIn]) -> frozenset[tuple[str, Verb]]:
    return frozenset((grant.resource, grant.verb) for grant in grants)


def _role_out(db: Database, role: Role) -> RoleOut:
    grants = [
        GrantOut(resource=p.resource, verb=p.verb)
        for p in RolePermissionRepository(db).list_for_role(role.id)
    ]
    return RoleOut.from_model(role, grants)


def _get_role_or_404(db: Database, role_id: str) -> Role:
    role = RoleRepository(db).get(role_id)
    if role is None:
        raise NotFoundError(f"no role {role_id!r}", details={"role_id": role_id})
    return role


def _list_roles(db: Database) -> list[RoleOut]:
    return [_role_out(db, role) for role in RoleRepository(db).list_all()]


def _create_role(
    db: Database, principal: Principal | None, payload: CreateRoleRequest
) -> RoleOut:
    grants = _to_grant_set(payload.permissions)
    assert_grants_grantable(db, grants)
    if principal is not None:
        assert_may_grant_permissions(db, principal, grants)
    role = RoleRepository(db).create(
        payload.slug, payload.name, payload.description, grants=grants
    )
    return _role_out(db, role)


def _update_role(
    db: Database,
    principal: Principal | None,
    role_id: str,
    payload: UpdateRoleRequest,
) -> RoleOut:
    role = _get_role_or_404(db, role_id)
    if role.builtin:
        raise BuiltinRoleError(f"role {role.slug!r} is builtin and cannot be edited")
    grants = _to_grant_set(payload.permissions)
    assert_grants_grantable(db, grants)
    if principal is not None:
        assert_may_grant_permissions(db, principal, grants)
    RolePermissionRepository(db).replace_for_role(role.id, grants)
    refreshed = RoleRepository(db).get(role.id)
    assert refreshed is not None  # unreachable — just updated, still in the same txn
    return _role_out(db, refreshed)


def _delete_role(db: Database, role_id: str) -> None:
    _get_role_or_404(db, role_id)
    RoleRepository(db).delete(role_id)  # BuiltinRoleError/RoleInUseError as needed


@router.get("", response_model=list[RoleOut], dependencies=[_require_read()])
async def list_roles(request: Request) -> list[RoleOut]:
    return await anyio.to_thread.run_sync(_list_roles, request.app.state.db)


@router.post(
    "", response_model=RoleOut, status_code=201, dependencies=[_require_write()]
)
async def create_role(request: Request, payload: CreateRoleRequest) -> RoleOut:
    return await anyio.to_thread.run_sync(
        _create_role, request.app.state.db, current_principal(request), payload
    )


@router.patch("/{role_id}", response_model=RoleOut, dependencies=[_require_write()])
async def update_role(
    request: Request, role_id: str, payload: UpdateRoleRequest
) -> RoleOut:
    return await anyio.to_thread.run_sync(
        _update_role,
        request.app.state.db,
        current_principal(request),
        role_id,
        payload,
    )


@router.delete("/{role_id}", status_code=204, dependencies=[_require_write()])
async def delete_role(request: Request, role_id: str) -> Response:
    await anyio.to_thread.run_sync(_delete_role, request.app.state.db, role_id)
    return Response(status_code=204)
