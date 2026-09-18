"""`GET`/`POST`/`DELETE /auth/role-mappings` — the IdP claim -> role catalog. See
issue #42.

`role_mappings` has shipped empty since M2 (#24) with no management API and no
uniqueness constraint. This is the write surface: `POST` maps one `(provider, claim,
value)` triple onto a role, `DELETE` removes a mapping by id, and `GET` lists them all
— nothing here calls `ansina.auth.role_sync.sync_mapped_roles` (that's #43's OIDC login
exchange, the primitive's only intended caller).

One resource, `auth.role_mappings` — no policy change needed, it falls under
`auth.policy`'s existing `auth.`-prefix rule (Maintain/Admin only, every mutation
additionally sudo-gated for Maintain via #37's fail-closed `authorize()`), the same
shape `routes/roles.py`/`role_assignments.py` already use. `POST` additionally treats a
mapping as a *deferred* role assignment and runs it through
`auth.management.assert_may_assign_role` against the mapped role — a sudo'd Maintain
cannot stage a mapping onto `admin` (or any role carrying grants it doesn't itself
hold) for a future login to apply; only `Admin` can. A submitted `role_id` that doesn't
exist is checked and refused (404) *before* the insert runs, so a typo is never
mistaken for `idx_role_mappings_tuple`'s own 409 — both `role_mappings.role_id` and
`role_assignments.role_id` are foreign keys, and letting a bad id reach the query would
raise the identical `sqlite3.IntegrityError` a real duplicate does.
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
from ansina.auth.management import NotFoundError, assert_may_assign_role
from ansina.auth.repositories import RoleMappingRepository, RoleRepository

if TYPE_CHECKING:
    from ansina.auth.models import Role, RoleMapping
    from ansina.auth.principal import Principal
    from ansina.storage.database import Database

router = APIRouter(prefix="/auth/role-mappings")

_RESOURCE = "auth.role_mappings"
_DESCRIPTION = (
    "IdP claim -> role mappings: which (provider, claim, value) triples resolve to "
    "which role on a claims-based login sync."
)


def _require_read() -> Depends:
    return Depends(require(_RESOURCE, description=_DESCRIPTION))


def _require_write() -> Depends:
    return Depends(require(_RESOURCE, description=_DESCRIPTION, sensitive=True))


class RoleMappingOut(BaseModel):
    id: str
    provider: str
    claim: str
    value: str
    role_id: str

    @classmethod
    def from_model(cls, mapping: RoleMapping) -> RoleMappingOut:
        return cls(
            id=mapping.id,
            provider=mapping.provider,
            claim=mapping.claim,
            value=mapping.value,
            role_id=mapping.role_id,
        )


class CreateRoleMappingRequest(BaseModel):
    provider: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    value: str = Field(min_length=1)
    role_id: str = Field(min_length=1)


def _get_role_or_404(db: Database, role_id: str) -> Role:
    role = RoleRepository(db).get(role_id)
    if role is None:
        raise NotFoundError(f"no role {role_id!r}", details={"role_id": role_id})
    return role


def _get_mapping_or_404(db: Database, mapping_id: str) -> RoleMapping:
    mapping = RoleMappingRepository(db).get(mapping_id)
    if mapping is None:
        raise NotFoundError(
            f"no role mapping {mapping_id!r}", details={"mapping_id": mapping_id}
        )
    return mapping


def _list_mappings(db: Database) -> list[RoleMappingOut]:
    return [
        RoleMappingOut.from_model(mapping)
        for mapping in RoleMappingRepository(db).list_all()
    ]


def _create_mapping(
    db: Database, principal: Principal | None, payload: CreateRoleMappingRequest
) -> RoleMappingOut:
    role = _get_role_or_404(db, payload.role_id)
    if principal is not None:
        assert_may_assign_role(db, principal, role)
    mapping = RoleMappingRepository(db).create(
        payload.provider, payload.claim, payload.value, payload.role_id
    )
    return RoleMappingOut.from_model(mapping)


def _delete_mapping(db: Database, mapping_id: str) -> None:
    _get_mapping_or_404(db, mapping_id)
    RoleMappingRepository(db).delete(mapping_id)


@router.get("", response_model=list[RoleMappingOut], dependencies=[_require_read()])
async def list_role_mappings(request: Request) -> list[RoleMappingOut]:
    return await anyio.to_thread.run_sync(_list_mappings, request.app.state.db)


@router.post(
    "", response_model=RoleMappingOut, status_code=201, dependencies=[_require_write()]
)
async def create_role_mapping(
    request: Request, payload: CreateRoleMappingRequest
) -> RoleMappingOut:
    return await anyio.to_thread.run_sync(
        _create_mapping, request.app.state.db, current_principal(request), payload
    )


@router.delete("/{mapping_id}", status_code=204, dependencies=[_require_write()])
async def delete_role_mapping(request: Request, mapping_id: str) -> Response:
    await anyio.to_thread.run_sync(_delete_mapping, request.app.state.db, mapping_id)
    return Response(status_code=204)
