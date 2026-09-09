"""User management. See issue #27, token routes narrowed by issue #28.

`GET` routes are `sensitive=False` — a `Maintain` caller can always list/inspect users
with no sudo grant. Every mutating route (`POST`/`PATCH`/`DELETE`, plus the three
credential routes) is `sensitive=True`, all sharing the `auth.users` resource so the
route-coverage audit's grant reconciliation treats them as one management surface.

`DELETE` is a one-way tombstone (`UserRepository.soft_delete`), not a row removal — see
`storage/migrations/0004_user_tombstone.sql`'s docstring for why. A tombstoned user is
treated as gone by every route here except the bare `GET /{id}` (which still shows it,
`deleted_at` included, for audit visibility): `PATCH`/`DELETE`/every credential route
(including the token routes' own `GET`) 404 on one.

The token routes (`ansina.api.tokens`, shared with `api.routes.me`'s self-service
surface) are Admin/sudo'd-Maintain-on-behalf-of. Issue #28 narrows the issuing one:
it mints a user's *first* `api_token` credential, never a second
(`ansina.auth.management.assert_no_existing_api_token`) — beyond that, the user mints
their own via `POST /auth/me/tokens`. Revoking a user's last token drops the count
back to zero, which is what makes this surface double as the lost-credential recovery
path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio.to_thread
from fastapi import APIRouter, Request
from fastapi.params import Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ansina.api import tokens as tokens_api
from ansina.api.authorization import require
from ansina.auth.clock import iso, utc_now
from ansina.auth.hashing import Argon2Params
from ansina.auth.management import (
    NotFoundError,
    assert_admin_remains,
    assert_no_existing_api_token,
    assert_not_bootstrap_identity,
)
from ansina.auth.repositories import CredentialRepository, UserRepository

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
    UserRepository(db).soft_delete(user.id, deleted_at=iso(utc_now()))


def _set_password(
    db: Database, settings: Settings, user_id: str, payload: SetPasswordRequest
) -> None:
    user = _get_live_user(db, user_id)
    CredentialRepository(db).set_password(
        user.id, payload.password, Argon2Params.from_settings(settings)
    )


def _issue_token(
    db: Database, user_id: str, payload: tokens_api.IssueTokenRequest
) -> tokens_api.IssuedTokenResponse:
    user = _get_live_user(db, user_id)
    # Invariant A (bootstrap identity) checked ahead of invariant B (already holds a
    # token) deliberately: the bootstrap identity always holds exactly one token from
    # first boot, so B would otherwise always win and its 409 message — "revoke it
    # first, or have the user mint their own via POST /auth/me/tokens" — is actively
    # wrong advice for an identity that can't reach either of those routes either.
    # `tokens_api.issue_token` re-checks this same guard; the duplication is cheap and
    # keeps that function's own invariant self-contained regardless of caller.
    assert_not_bootstrap_identity(db, user.id)
    assert_no_existing_api_token(db, user.id)  # issue #28's invariant B
    return tokens_api.issue_token(db, user.id, payload.label)


def _list_tokens(db: Database, user_id: str) -> list[tokens_api.TokenOut]:
    user = _get_live_user(db, user_id)
    return tokens_api.list_tokens(db, user.id)


def _revoke_token(db: Database, user_id: str, token_id: str) -> None:
    user = _get_live_user(db, user_id)
    tokens_api.revoke_token(db, user.id, token_id)


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
    response_model=tokens_api.IssuedTokenResponse,
    status_code=201,
    dependencies=[_require_write()],
)
async def issue_token(
    request: Request, user_id: str, payload: tokens_api.IssueTokenRequest
) -> tokens_api.IssuedTokenResponse:
    return await anyio.to_thread.run_sync(
        _issue_token, request.app.state.db, user_id, payload
    )


@router.get(
    "/{user_id}/tokens",
    response_model=list[tokens_api.TokenOut],
    dependencies=[_require_read()],
)
async def list_user_tokens(request: Request, user_id: str) -> list[tokens_api.TokenOut]:
    return await anyio.to_thread.run_sync(_list_tokens, request.app.state.db, user_id)


@router.delete(
    "/{user_id}/tokens/{token_id}",
    status_code=204,
    dependencies=[_require_write()],
)
async def revoke_user_token(request: Request, user_id: str, token_id: str) -> Response:
    await anyio.to_thread.run_sync(
        _revoke_token, request.app.state.db, user_id, token_id
    )
    return Response(status_code=204)
