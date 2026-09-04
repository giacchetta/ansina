"""Role attach/detach for users and groups. See issue #27.

One router, one `auth.role_assignments` resource, all four routes `sensitive=True` —
granting or revoking a role is never a `Read`/`Write`-safe action and never something
`Maintain` does without a live sudo grant.

Every assignment additionally runs through `auth.management.assert_may_assign_role`:
a caller can never grant a role carrying permissions it does not itself effectively
hold, checked against the resolved `Principal` on the request — skipped only when
`security.enabled = False` leaves no `Principal` to check at all (dev mode, where
`require()` itself already no-ops).

Detaching `admin` from a user, or from a group that carries it, additionally runs
through `auth.management.assert_admin_remains` — the same last-Admin guard `routes/
users.py` and `routes/groups.py` apply on delete/deactivate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio.to_thread
from fastapi import APIRouter, Request
from fastapi.params import Depends
from fastapi.responses import Response

from ansina.api.authorization import require
from ansina.auth.management import (
    NotFoundError,
    assert_admin_remains,
    assert_may_assign_role,
)
from ansina.auth.models import RoleSlug, SubjectType
from ansina.auth.repositories import (
    GroupRepository,
    RoleAssignmentRepository,
    RoleRepository,
)

if TYPE_CHECKING:
    from ansina.auth.models import Role
    from ansina.auth.principal import Principal
    from ansina.storage.database import Database

router = APIRouter(prefix="/auth")

_RESOURCE = "auth.role_assignments"
_DESCRIPTION = "Attach/detach a role to a user or a group."


def _require_write() -> Depends:
    return Depends(require(_RESOURCE, description=_DESCRIPTION, sensitive=True))


def _principal(request: Request) -> Principal | None:
    """`request.state.principal` if one was resolved, else `None` — mirrors `routes/
    sudo.py`'s own helper; `security.enabled = false` never sets it at all.
    """
    return getattr(request.state, "principal", None)


def _get_role_or_404(db: Database, role_id: str) -> Role:
    role = RoleRepository(db).get(role_id)
    if role is None:
        raise NotFoundError(f"no role {role_id!r}", details={"role_id": role_id})
    return role


def _assign(
    db: Database,
    principal: Principal | None,
    subject_type: SubjectType,
    subject_id: str,
    role_id: str,
) -> None:
    role = _get_role_or_404(db, role_id)
    if principal is not None:
        assert_may_assign_role(db, principal, role)
    RoleAssignmentRepository(db).assign(subject_type, subject_id, role.id)


def _unassign_from_user(db: Database, user_id: str, role_id: str) -> None:
    role = _get_role_or_404(db, role_id)
    if role.slug == RoleSlug.ADMIN.value:
        assert_admin_remains(db, frozenset({user_id}))
    RoleAssignmentRepository(db).unassign(SubjectType.USER, user_id, role.id)


def _unassign_from_group(db: Database, group_id: str, role_id: str) -> None:
    role = _get_role_or_404(db, role_id)
    if role.slug == RoleSlug.ADMIN.value:
        members = GroupRepository(db).list_members(group_id)
        assert_admin_remains(db, frozenset(member.id for member in members))
    RoleAssignmentRepository(db).unassign(SubjectType.GROUP, group_id, role.id)


@router.post(
    "/users/{user_id}/roles/{role_id}", status_code=204, dependencies=[_require_write()]
)
async def assign_user_role(request: Request, user_id: str, role_id: str) -> Response:
    await anyio.to_thread.run_sync(
        _assign,
        request.app.state.db,
        _principal(request),
        SubjectType.USER,
        user_id,
        role_id,
    )
    return Response(status_code=204)


@router.delete(
    "/users/{user_id}/roles/{role_id}", status_code=204, dependencies=[_require_write()]
)
async def unassign_user_role(request: Request, user_id: str, role_id: str) -> Response:
    await anyio.to_thread.run_sync(
        _unassign_from_user, request.app.state.db, user_id, role_id
    )
    return Response(status_code=204)


@router.post(
    "/groups/{group_id}/roles/{role_id}",
    status_code=204,
    dependencies=[_require_write()],
)
async def assign_group_role(request: Request, group_id: str, role_id: str) -> Response:
    await anyio.to_thread.run_sync(
        _assign,
        request.app.state.db,
        _principal(request),
        SubjectType.GROUP,
        group_id,
        role_id,
    )
    return Response(status_code=204)


@router.delete(
    "/groups/{group_id}/roles/{role_id}",
    status_code=204,
    dependencies=[_require_write()],
)
async def unassign_group_role(
    request: Request, group_id: str, role_id: str
) -> Response:
    await anyio.to_thread.run_sync(
        _unassign_from_group, request.app.state.db, group_id, role_id
    )
    return Response(status_code=204)
