"""Group management. See issue #27.

Same shape as `routes/users.py`: `GET` is `sensitive=False`, every mutation (create,
update, delete, membership add/remove) is `sensitive=True`, all sharing the
`auth.groups` resource.

Deleting a group, or removing a member from one, only touches last-Admin protection when
that group itself carries the `admin` role — a group with no `admin` grant can never be
the thing standing between the system and zero admins, so the guard is skipped entirely
in that (common) case rather than paying for a query that can't fail.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio.to_thread
from fastapi import APIRouter, Request
from fastapi.params import Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ansina.api.authorization import require
from ansina.auth.management import NotFoundError, assert_admin_remains
from ansina.auth.models import RoleSlug, SubjectType
from ansina.auth.repositories import GroupRepository, RoleAssignmentRepository

if TYPE_CHECKING:
    from ansina.auth.models import Group
    from ansina.storage.database import Database

router = APIRouter(prefix="/auth/groups")

_RESOURCE = "auth.groups"
_DESCRIPTION = "Groups: list/inspect, create, update, delete, and manage membership."


def _require_read() -> Depends:
    return Depends(require(_RESOURCE, description=_DESCRIPTION))


def _require_write() -> Depends:
    return Depends(require(_RESOURCE, description=_DESCRIPTION, sensitive=True))


class GroupOut(BaseModel):
    id: str
    slug: str
    name: str
    description: str
    created_at: str

    @classmethod
    def from_model(cls, group: Group) -> GroupOut:
        return cls(
            id=group.id,
            slug=group.slug,
            name=group.name,
            description=group.description,
            created_at=group.created_at,
        )


class CreateGroupRequest(BaseModel):
    slug: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""


class UpdateGroupRequest(BaseModel):
    name: str
    description: str = ""


def _get_group_or_404(db: Database, group_id: str) -> Group:
    group = GroupRepository(db).get(group_id)
    if group is None:
        raise NotFoundError(f"no group {group_id!r}", details={"group_id": group_id})
    return group


def _guard_admin_group_delete(db: Database, group_id: str) -> None:
    """Only relevant if `group_id` itself carries the `admin` role — see module
    docstring.
    """
    roles = RoleAssignmentRepository(db).list_for_subject(SubjectType.GROUP, group_id)
    if not any(role.slug == RoleSlug.ADMIN.value for role in roles):
        return
    members = GroupRepository(db).list_members(group_id)
    assert_admin_remains(db, frozenset(member.id for member in members))


def _list_groups(db: Database) -> list[GroupOut]:
    return [GroupOut.from_model(group) for group in GroupRepository(db).list_all()]


def _get_group(db: Database, group_id: str) -> GroupOut:
    return GroupOut.from_model(_get_group_or_404(db, group_id))


def _create_group(db: Database, payload: CreateGroupRequest) -> GroupOut:
    group = GroupRepository(db).create(
        payload.slug, payload.name, description=payload.description
    )
    return GroupOut.from_model(group)


def _update_group(db: Database, group_id: str, payload: UpdateGroupRequest) -> GroupOut:
    _get_group_or_404(db, group_id)
    updated = GroupRepository(db).update(
        group_id, name=payload.name, description=payload.description
    )
    assert updated is not None  # unreachable — existence just checked above
    return GroupOut.from_model(updated)


def _delete_group(db: Database, group_id: str) -> None:
    _get_group_or_404(db, group_id)
    _guard_admin_group_delete(db, group_id)
    GroupRepository(db).delete(group_id)


def _add_member(db: Database, group_id: str, user_id: str) -> None:
    _get_group_or_404(db, group_id)
    GroupRepository(db).add_member(group_id, user_id)


def _remove_member(db: Database, group_id: str, user_id: str) -> None:
    _get_group_or_404(db, group_id)
    roles = RoleAssignmentRepository(db).list_for_subject(SubjectType.GROUP, group_id)
    if any(role.slug == RoleSlug.ADMIN.value for role in roles):
        assert_admin_remains(db, frozenset({user_id}))
    GroupRepository(db).remove_member(group_id, user_id)


@router.get("", response_model=list[GroupOut], dependencies=[_require_read()])
async def list_groups(request: Request) -> list[GroupOut]:
    return await anyio.to_thread.run_sync(_list_groups, request.app.state.db)


@router.get("/{group_id}", response_model=GroupOut, dependencies=[_require_read()])
async def get_group(request: Request, group_id: str) -> GroupOut:
    return await anyio.to_thread.run_sync(_get_group, request.app.state.db, group_id)


@router.post(
    "", response_model=GroupOut, status_code=201, dependencies=[_require_write()]
)
async def create_group(request: Request, payload: CreateGroupRequest) -> GroupOut:
    return await anyio.to_thread.run_sync(_create_group, request.app.state.db, payload)


@router.patch("/{group_id}", response_model=GroupOut, dependencies=[_require_write()])
async def update_group(
    request: Request, group_id: str, payload: UpdateGroupRequest
) -> GroupOut:
    return await anyio.to_thread.run_sync(
        _update_group, request.app.state.db, group_id, payload
    )


@router.delete("/{group_id}", status_code=204, dependencies=[_require_write()])
async def delete_group(request: Request, group_id: str) -> Response:
    await anyio.to_thread.run_sync(_delete_group, request.app.state.db, group_id)
    return Response(status_code=204)


@router.put(
    "/{group_id}/members/{user_id}", status_code=204, dependencies=[_require_write()]
)
async def add_member(request: Request, group_id: str, user_id: str) -> Response:
    await anyio.to_thread.run_sync(_add_member, request.app.state.db, group_id, user_id)
    return Response(status_code=204)


@router.delete(
    "/{group_id}/members/{user_id}", status_code=204, dependencies=[_require_write()]
)
async def remove_member(request: Request, group_id: str, user_id: str) -> Response:
    await anyio.to_thread.run_sync(
        _remove_member, request.app.state.db, group_id, user_id
    )
    return Response(status_code=204)
