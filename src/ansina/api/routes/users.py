"""User management. See issue #27.

`GET` routes are `sensitive=False` — a `Maintain` caller can always list/inspect users
with no sudo grant. Every mutating route (`POST`/`PATCH`/`DELETE`, plus the two
credential routes) is `sensitive=True`, all sharing the `auth.users` resource so the
route-coverage audit's grant reconciliation treats them as one management surface.

`DELETE` is a one-way tombstone (`UserRepository.soft_delete`), not a row removal — see
`storage/migrations/0004_user_tombstone.sql`'s docstring for why. A tombstoned user is
treated as gone by every route here except `GET` (which still shows it, `deleted_at`
included, for audit visibility): `PATCH`/`DELETE`/the credential routes all 404 on one.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import anyio.to_thread
from fastapi import APIRouter, Request
from fastapi.params import Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ansina.api.authorization import require
from ansina.auth.hashing import Argon2Params
from ansina.auth.management import NotFoundError, assert_admin_remains
from ansina.auth.repositories import CredentialRepository, UserRepository
from ansina.logging import register_secret

if TYPE_CHECKING:
    from ansina.auth.models import User
    from ansina.config.settings import Settings
    from ansina.storage.database import Database

router = APIRouter(prefix="/auth/users")

_RESOURCE = "auth.users"
_DESCRIPTION = (
    "User accounts: list/inspect, create, update, delete, and manage their password "
    "and API tokens."
)


def _require_read() -> Depends:
    return Depends(require(_RESOURCE, description=_DESCRIPTION))


def _require_write() -> Depends:
    return Depends(require(_RESOURCE, description=_DESCRIPTION, sensitive=True))


def _now_iso() -> str:
    """Millisecond-precision ISO 8601 UTC, matching `auth.sudo`'s own `_iso` format and
    `0002_rbac.sql`'s column defaults closely enough to sort identically as text.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class UserOut(BaseModel):
    id: str
    username: str
    display_name: str
    active: bool
    created_at: str
    deleted_at: str | None = None

    @classmethod
    def from_model(cls, user: User) -> UserOut:
        return cls(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            active=user.active,
            created_at=user.created_at,
            deleted_at=user.deleted_at,
        )


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1)
    display_name: str = ""
    password: str | None = None


class UpdateUserRequest(BaseModel):
    display_name: str | None = None
    active: bool | None = None


class SetPasswordRequest(BaseModel):
    password: str = Field(min_length=1)


class IssueTokenRequest(BaseModel):
    label: str = ""


class IssuedTokenResponse(BaseModel):
    """The raw token, visible exactly once — never recoverable afterward, same
    discipline as `auth.bootstrap`'s bootstrap-token banner and `POST /auth/sudo`'s
    grant token.
    """

    token: str
    label: str


def _get_live_user(db: Database, user_id: str) -> User:
    """A user that still exists and hasn't been tombstoned — every mutating route below
    treats a deleted user as gone, not as a live 200/204 target.
    """
    user = UserRepository(db).get(user_id)
    if user is None or user.deleted_at is not None:
        raise NotFoundError(f"no user {user_id!r}", details={"user_id": user_id})
    return user


def _list_users(db: Database) -> list[UserOut]:
    return [UserOut.from_model(user) for user in UserRepository(db).list_all()]


def _get_user(db: Database, user_id: str) -> UserOut:
    user = UserRepository(db).get(user_id)
    if user is None:
        raise NotFoundError(f"no user {user_id!r}", details={"user_id": user_id})
    return UserOut.from_model(user)


def _create_user(
    db: Database, settings: Settings, payload: CreateUserRequest
) -> UserOut:
    user = UserRepository(db).create(
        payload.username, display_name=payload.display_name
    )
    if payload.password is not None:
        CredentialRepository(db).set_password(
            user.id, payload.password, Argon2Params.from_settings(settings)
        )
    return UserOut.from_model(user)


def _update_user(db: Database, user_id: str, payload: UpdateUserRequest) -> UserOut:
    user = _get_live_user(db, user_id)
    users = UserRepository(db)
    if payload.active is False:
        assert_admin_remains(db, frozenset({user.id}))
    if payload.display_name is not None:
        users.set_display_name(user.id, payload.display_name)
    if payload.active is not None:
        users.set_active(user.id, active=payload.active)
    refreshed = users.get(user.id)
    assert refreshed is not None  # unreachable — just updated, still in the same txn
    return UserOut.from_model(refreshed)


def _delete_user(db: Database, user_id: str) -> None:
    user = _get_live_user(db, user_id)
    assert_admin_remains(db, frozenset({user.id}))
    UserRepository(db).soft_delete(user.id, deleted_at=_now_iso())


def _set_password(
    db: Database, settings: Settings, user_id: str, payload: SetPasswordRequest
) -> None:
    user = _get_live_user(db, user_id)
    CredentialRepository(db).set_password(
        user.id, payload.password, Argon2Params.from_settings(settings)
    )


def _issue_token(
    db: Database, user_id: str, payload: IssueTokenRequest
) -> IssuedTokenResponse:
    user = _get_live_user(db, user_id)
    token = secrets.token_urlsafe(32)
    register_secret(token)
    CredentialRepository(db).create_api_token(user.id, token, label=payload.label)
    return IssuedTokenResponse(token=token, label=payload.label)


@router.get("", response_model=list[UserOut], dependencies=[_require_read()])
async def list_users(request: Request) -> list[UserOut]:
    return await anyio.to_thread.run_sync(_list_users, request.app.state.db)


@router.get("/{user_id}", response_model=UserOut, dependencies=[_require_read()])
async def get_user(request: Request, user_id: str) -> UserOut:
    return await anyio.to_thread.run_sync(_get_user, request.app.state.db, user_id)


@router.post(
    "", response_model=UserOut, status_code=201, dependencies=[_require_write()]
)
async def create_user(request: Request, payload: CreateUserRequest) -> UserOut:
    return await anyio.to_thread.run_sync(
        _create_user, request.app.state.db, request.app.state.settings, payload
    )


@router.patch("/{user_id}", response_model=UserOut, dependencies=[_require_write()])
async def update_user(
    request: Request, user_id: str, payload: UpdateUserRequest
) -> UserOut:
    return await anyio.to_thread.run_sync(
        _update_user, request.app.state.db, user_id, payload
    )


@router.delete("/{user_id}", status_code=204, dependencies=[_require_write()])
async def delete_user(request: Request, user_id: str) -> Response:
    await anyio.to_thread.run_sync(_delete_user, request.app.state.db, user_id)
    return Response(status_code=204)


@router.put("/{user_id}/password", status_code=204, dependencies=[_require_write()])
async def set_password(
    request: Request, user_id: str, payload: SetPasswordRequest
) -> Response:
    db = request.app.state.db
    settings = request.app.state.settings
    await anyio.to_thread.run_sync(_set_password, db, settings, user_id, payload)
    return Response(status_code=204)


@router.post(
    "/{user_id}/tokens",
    response_model=IssuedTokenResponse,
    status_code=201,
    dependencies=[_require_write()],
)
async def issue_token(
    request: Request, user_id: str, payload: IssueTokenRequest
) -> IssuedTokenResponse:
    return await anyio.to_thread.run_sync(
        _issue_token, request.app.state.db, user_id, payload
    )
