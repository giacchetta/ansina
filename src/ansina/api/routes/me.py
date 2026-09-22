"""Self-identity: `GET /auth/me` (issue #30), self-service tokens (issue #28),
self-service TOTP enrollment (issue #41), and self-service password change (issue #48).

The resources declared here are `me.profile`, `me.tokens`, `me.totp`, and `me.password`
while the paths are `/auth/me`, `/auth/me/tokens`, `/auth/me/totp`, and
`/auth/me/password` — that is consistent, not sloppy. A resource is a stable, dotted,
**URL-independent** name (`docs/architecture/blueprint.md` §Identity & access control);
the policy has always keyed on the resource, never the path.

All four fall under `auth.policy`'s `me.*` carve-out (`is_self_resource`): every
builtin role — `Read` included — holds every verb on all four, so no route here is
ever `ForbiddenError`-blocked by role alone. This is not an escalation: the subject of
a `me.*` action is always the authenticated caller themselves, resolved once by
`BearerAuthMiddleware` onto `request.state.principal`, so no route here can reach
another user's data. `GET /auth/me` reads everything but `step_up_factors` from the
already-resolved `Principal` — that one field costs a single indexed `credentials`
read via `SudoService.enrolled_factors` (issue #37), since enrollment isn't part of
what `resolve_principal` resolves. The token, TOTP, and password routes also touch the
database (`ansina.api.tokens`, shared with `api.routes.users`'s admin-on-behalf-of
surface; `ansina.auth.encryption`/`ansina.auth.totp`; and
`ansina.auth.password_policy`/`CredentialRepository.set_password` respectively),
through `anyio.to_thread.run_sync` like every other DB-touching route.

One caller is refused on the token routes regardless of role: the synthetic bootstrap
identity, which holds exactly one `api_token`, ever — `POST`/`DELETE` 403
(`ansina.auth.bootstrap_identity`); see `ansina.auth.bootstrap`'s module docstring for
why. `GET` is unaffected — the bootstrap identity can still see its one token's
metadata.

Unlike `me.profile`/`me.tokens`, `me.totp` is the one `me.*` resource with a
`sensitive=True` route (`DELETE`) — `authorize()`'s sensitivity check is a per-`
require()`-call flag, not a pattern match on the resource name, so a `me.*` resource
can still demand a live sudo grant on the one verb that needs it (issue #41 AC:
disabling a factor requires sudo; enrolling one deliberately cannot, or a
password-less user could never obtain its first factor at all).

`me.password`'s own `PUT` is deliberately never `sensitive=True` either, for a
different reason than `me.totp`'s enrollment: the request body's own
`current_password` check *is* the proof-of-possession — the identical one
`PasswordStepUpVerifier` performs at `POST /auth/sudo` — so requiring a live sudo grant
on top would mean requiring the password to change the password. A caller holding no
password credential yet (the default shape of any user `POST /auth/users` creates
without one) may set a first one with `current_password` omitted — the same
chicken-and-egg carve-out `POST /auth/me/totp` already makes for a first step-up
factor, since the caller already holds a bearer token for this account and confers no
new authority on itself. Enforced by `ansina.auth.password_policy
.assert_password_acceptable`, the same as every other password-setting path
(`api.routes.users`).

`security.enabled = false` never resolves a `Principal` at all; handled the same way
`routes/sudo.py`/`routes/role_assignments.py` already do, via `api.identity`.
"""

from __future__ import annotations

import base64
import os
from typing import TYPE_CHECKING
from urllib.parse import quote

import anyio.to_thread
from fastapi import APIRouter, Request
from fastapi.params import Depends
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from ansina.api import tokens as tokens_api
from ansina.api.authorization import require
from ansina.api.identity import current_principal, no_identity_response
from ansina.api.problems import CODE_UNAUTHORIZED, problem_response
from ansina.auth import totp
from ansina.auth.encryption import EncryptionKeyMissingError, encrypt, resolve_key
from ansina.auth.hashing import Argon2Params
from ansina.auth.management import assert_totp_not_enrolled
from ansina.auth.models import CredentialType
from ansina.auth.password_policy import assert_password_acceptable
from ansina.auth.repositories import CredentialRepository

if TYPE_CHECKING:
    from ansina.auth.principal import Principal
    from ansina.config.settings import Settings
    from ansina.storage.database import Database

router = APIRouter(prefix="/auth")

_RESOURCE = "me.profile"
_DESCRIPTION = "The authenticated caller's own identity: user, roles, sudo status."

_TOKENS_RESOURCE = "me.tokens"
_TOKENS_DESCRIPTION = (
    "The authenticated caller's own API tokens: mint, list, and revoke."
)

_TOTP_RESOURCE = "me.totp"
_TOTP_DESCRIPTION = (
    "The authenticated caller's own TOTP second factor: enroll, check status, and "
    "disable."
)
_TOTP_ISSUER = "Ansina"

_PASSWORD_RESOURCE = "me.password"
_PASSWORD_DESCRIPTION = "The authenticated caller's own password: set or change it."


def _require_read() -> Depends:
    return Depends(require(_RESOURCE, description=_DESCRIPTION))


def _require_tokens() -> Depends:
    return Depends(require(_TOKENS_RESOURCE, description=_TOKENS_DESCRIPTION))


def _require_totp() -> Depends:
    """Not `sensitive=True` — issue #41's own AC: enrollment can't require the sudo
    grant TOTP itself is meant to provide, a chicken-and-egg lockout for exactly the
    password-less population that needs it. Also used for the status `GET`, which has
    nothing sensitive to gate at all.
    """
    return Depends(require(_TOTP_RESOURCE, description=_TOTP_DESCRIPTION))


def _require_totp_disable() -> Depends:
    """`sensitive=True`: a stolen bearer token must not be able to remove the
    caller's own second factor — issue #41 AC.
    """
    return Depends(
        require(_TOTP_RESOURCE, description=_TOTP_DESCRIPTION, sensitive=True)
    )


def _require_password() -> Depends:
    """Not `sensitive=True` — see the module docstring's `me.password` paragraph: the
    request body's own `current_password` check is the proof-of-possession, the same
    one a sudo grant would otherwise exist to provide.
    """
    return Depends(require(_PASSWORD_RESOURCE, description=_PASSWORD_DESCRIPTION))


class MeOut(BaseModel):
    user_id: str
    username: str
    display_name: str
    roles: list[str]
    auth_method: str
    sudo_active: bool
    step_up_factors: list[str]


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

    step_up_factors = await anyio.to_thread.run_sync(
        request.app.state.sudo.enrolled_factors, principal
    )
    body = MeOut(
        user_id=principal.user.id,
        username=principal.user.username,
        display_name=principal.user.display_name,
        roles=sorted(principal.role_slugs),
        auth_method=principal.auth_method.value,
        sudo_active=principal.sudo_active,
        step_up_factors=step_up_factors,
    )
    return JSONResponse(status_code=200, content=body.model_dump())


@router.post(
    "/me/tokens",
    response_model=None,
    responses={
        201: {"model": tokens_api.IssuedTokenResponse},
        401: {"description": "No resolved identity."},
        403: {
            "description": "The bootstrap identity — see the module docstring for why."
        },
    },
    status_code=201,
    dependencies=[_require_tokens()],
)
async def mint_own_token(
    request: Request, payload: tokens_api.IssueTokenRequest
) -> JSONResponse:
    principal = current_principal(request)
    if principal is None:
        return no_identity_response()

    issued = await anyio.to_thread.run_sync(
        tokens_api.issue_token, request.app.state.db, principal.user.id, payload.label
    )
    return JSONResponse(status_code=201, content=issued.model_dump())


@router.get(
    "/me/tokens",
    response_model=None,
    responses={
        200: {"model": list[tokens_api.TokenOut]},
        401: {"description": "No resolved identity."},
    },
    dependencies=[_require_tokens()],
)
async def list_own_tokens(request: Request) -> JSONResponse:
    principal = current_principal(request)
    if principal is None:
        return no_identity_response()

    listed = await anyio.to_thread.run_sync(
        tokens_api.list_tokens, request.app.state.db, principal.user.id
    )
    return JSONResponse(
        status_code=200, content=[token.model_dump() for token in listed]
    )


@router.delete(
    "/me/tokens/{token_id}",
    response_model=None,
    responses={
        204: {"description": "Revoked."},
        401: {"description": "No resolved identity."},
        403: {
            "description": "The bootstrap identity — see the module docstring for why."
        },
        404: {"description": "No such token id for the caller's own account."},
    },
    dependencies=[_require_tokens()],
)
async def revoke_own_token(request: Request, token_id: str) -> Response:
    principal = current_principal(request)
    if principal is None:
        return no_identity_response()

    await anyio.to_thread.run_sync(
        tokens_api.revoke_token, request.app.state.db, principal.user.id, token_id
    )
    return Response(status_code=204)


# --- /auth/me/totp: issue #41's self-service TOTP surface ---------------------------


class TotpEnrollResponse(BaseModel):
    """The freshly generated secret — visible exactly once, at enrollment, same
    discipline as `IssuedTokenResponse.token` and the bootstrap-token banner. Never
    recoverable afterward: only the AES-GCM envelope is stored (`auth.encryption`).
    """

    secret: str
    otpauth_uri: str
    digits: int
    period_seconds: int


class TotpStatusResponse(BaseModel):
    enrolled: bool
    enrolled_since: str | None


def _otpauth_uri(username: str, secret_b32: str) -> str:
    """A standard `otpauth://` URI (Key Uri Format, as implemented by every mainstream
    authenticator app) — lets an enroll response drive a QR code as well as manual
    entry, from the same call.
    """
    label = quote(f"{_TOTP_ISSUER}:{username}")
    return (
        f"otpauth://totp/{label}?secret={secret_b32}&issuer={quote(_TOTP_ISSUER)}"
        f"&digits={totp.DEFAULT_DIGITS}&period={totp.DEFAULT_STEP_SECONDS}"
    )


def _enroll_totp(
    db: Database, settings: Settings, principal: Principal
) -> TotpEnrollResponse:
    """Refuses (`TotpAlreadyEnrolledError`, 409) a second enrollment, and
    (`EncryptionKeyMissingError`, 503) if `[security.encryption] key` isn't
    configured — both checked before a secret is ever generated.
    """
    assert_totp_not_enrolled(db, principal.user.id)
    key = resolve_key(settings)
    if key is None:
        raise EncryptionKeyMissingError(
            "cannot enroll TOTP — [security.encryption] key is not configured"
        )
    secret = os.urandom(totp.SECRET_BYTES)
    secret_b32 = base64.b32encode(secret).decode("ascii").rstrip("=")
    CredentialRepository(db).create_totp_secret(principal.user.id, encrypt(secret, key))
    return TotpEnrollResponse(
        secret=secret_b32,
        otpauth_uri=_otpauth_uri(principal.user.username, secret_b32),
        digits=totp.DEFAULT_DIGITS,
        period_seconds=totp.DEFAULT_STEP_SECONDS,
    )


def _totp_status(db: Database, principal: Principal) -> TotpStatusResponse:
    credential = CredentialRepository(db).get_totp_secret(principal.user.id)
    return TotpStatusResponse(
        enrolled=credential is not None,
        enrolled_since=credential.created_at if credential is not None else None,
    )


def _disable_totp(db: Database, user_id: str) -> None:
    """Idempotent, like `SudoService.revoke_for_user` — disabling a factor that was
    never (or no longer) enrolled is still a 204, not a 404; there is nothing left to
    tell the caller that a re-check of `GET /auth/me/totp` wouldn't already show.
    """
    CredentialRepository(db).delete_credentials(user_id, CredentialType.TOTP)


@router.post(
    "/me/totp",
    response_model=None,
    responses={
        201: {"model": TotpEnrollResponse},
        401: {"description": "No resolved identity."},
        409: {"description": "Already enrolled — disable it first."},
        503: {
            "description": "[security.encryption] key is not configured — TOTP "
            "cannot be enrolled."
        },
    },
    status_code=201,
    dependencies=[_require_totp()],
)
async def enroll_totp(request: Request) -> JSONResponse:
    principal = current_principal(request)
    if principal is None:
        return no_identity_response()

    enrolled = await anyio.to_thread.run_sync(
        _enroll_totp, request.app.state.db, request.app.state.settings, principal
    )
    return JSONResponse(status_code=201, content=enrolled.model_dump())


@router.get(
    "/me/totp",
    response_model=None,
    responses={
        200: {"model": TotpStatusResponse},
        401: {"description": "No resolved identity."},
    },
    dependencies=[_require_totp()],
)
async def get_totp_status(request: Request) -> JSONResponse:
    principal = current_principal(request)
    if principal is None:
        return no_identity_response()

    status = await anyio.to_thread.run_sync(
        _totp_status, request.app.state.db, principal
    )
    return JSONResponse(status_code=200, content=status.model_dump())


@router.delete(
    "/me/totp",
    status_code=204,
    responses={
        204: {"description": "Disabled (or was never enrolled — idempotent)."},
        401: {"description": "No resolved identity."},
        403: {"description": "No live sudo grant."},
    },
    dependencies=[_require_totp_disable()],
)
async def disable_totp(request: Request) -> Response:
    principal = current_principal(request)
    if principal is None:
        return no_identity_response()

    await anyio.to_thread.run_sync(
        _disable_totp, request.app.state.db, principal.user.id
    )
    return Response(status_code=204)


# --- /auth/me/password: issue #48's self-service password change --------------------


class ChangePasswordRequest(BaseModel):
    """`current_password` is optional only for the first-password carve-out — see the
    module docstring's `me.password` paragraph. A caller who already holds a password
    credential and omits it simply fails proof-of-possession (401), the same as
    presenting a wrong one; it is never a distinct error shape, so a probing caller
    can't use the presence/absence of the field to learn whether an account has a
    password without proving one it does.
    """

    current_password: str | None = None
    new_password: str


def _change_own_password(
    db: Database,
    settings: Settings,
    principal: Principal,
    payload: ChangePasswordRequest,
) -> bool:
    """Returns `True` on success, `False` on a failed proof-of-possession — the caller
    (`change_own_password` below) turns `False` into the same 401 shape
    `POST /auth/sudo` already uses for a failed step-up. Policy
    (`assert_password_acceptable`) is checked *after* proof-of-possession succeeds, so
    a caller who can't prove they hold the account never gets to probe the password
    policy.
    """
    credentials = CredentialRepository(db)
    holds_password = credentials.has_credential(
        principal.user.id, CredentialType.PASSWORD
    )
    params = Argon2Params.from_settings(settings)

    # The first-password carve-out (no `else` branch needed): a caller holding no
    # password credential yet already holds a bearer token for this account, so
    # setting a first password confers no new authority regardless of whether
    # `current_password` was supplied.
    if holds_password and (
        payload.current_password is None
        or not credentials.verify_password(
            principal.user.id, payload.current_password, params
        )
    ):
        return False

    assert_password_acceptable(
        payload.new_password, username=principal.user.username, settings=settings
    )
    credentials.set_password(principal.user.id, payload.new_password, params)
    return True


@router.put(
    "/me/password",
    status_code=204,
    responses={
        204: {"description": "Password set/changed."},
        400: {
            "description": "The new password fails policy (too short, too long, "
            "contains the username, or on the common-password list)."
        },
        401: {
            "description": "No resolved identity, or `current_password` is missing/"
            "wrong for a caller who already holds a password credential."
        },
    },
    dependencies=[_require_password()],
)
async def change_own_password(
    request: Request, payload: ChangePasswordRequest
) -> Response:
    principal = current_principal(request)
    if principal is None:
        return no_identity_response()

    ok = await anyio.to_thread.run_sync(
        _change_own_password,
        request.app.state.db,
        request.app.state.settings,
        principal,
        payload,
    )
    if not ok:
        return problem_response(
            status=401,
            code=CODE_UNAUTHORIZED,
            title="Unauthorized",
            detail="current_password is missing or does not match.",
        )
    return Response(status_code=204)
