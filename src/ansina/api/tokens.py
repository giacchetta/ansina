"""Shared self-service token surface. See issue #28.

`TokenOut`/`IssueTokenRequest`/`IssuedTokenResponse` and the three DB-facing helpers
(`issue_token`, `list_tokens`, `revoke_token`) are what `api.routes.me`'s
`me.tokens` routes and `api.routes.users`'s admin-on-behalf-of token routes both build
on — lifted out the same way `api.identity` was (issue #30) once a second router
needed the exact same shape.

`assert_not_bootstrap_identity` (issue #28's invariant A — the bootstrap identity
holds exactly one `api_token`, ever) is enforced here, in both `issue_token` and
`revoke_token`, so every caller inherits it rather than needing to remember it. This
module does *not* enforce issue #28's invariant B (an Admin issues a user's *first*
credential, never a second) — that's a rule about who may call
`POST /auth/users/{id}/tokens`, not about the token surface itself, since a user is
always free to hold many of their own self-minted tokens. `api.routes.users` applies
it itself, one call to `auth.management.assert_no_existing_api_token` ahead of this
module's `issue_token`.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel

from ansina.auth.clock import Clock, iso, utc_now
from ansina.auth.management import NotFoundError, assert_not_bootstrap_identity
from ansina.auth.repositories import CredentialRepository
from ansina.logging import register_secret

if TYPE_CHECKING:
    from ansina.auth.models import Credential
    from ansina.storage.database import Database

# 32 raw bytes -> 43 base64url characters, the same generation shape
# `auth.bootstrap`'s bootstrap token already uses.
_GENERATED_TOKEN_BYTES = 32


class TokenOut(BaseModel):
    """One token's metadata — never `hash`/`salt`. `expires_at` (issue #39) is `null`
    for a token that never expires — every M2/M4-era token, and still the default for
    every self- and admin-issued token today — or the ISO 8601 instant past which
    `ansina.auth.authenticator.ApiTokenAuthenticator` stops matching it.
    """

    id: str
    label: str
    created_at: str
    last_used_at: str | None
    expires_at: str | None

    @classmethod
    def from_model(cls, credential: Credential) -> TokenOut:
        return cls(
            id=credential.id,
            label=credential.label,
            created_at=credential.created_at,
            last_used_at=credential.last_used_at,
            expires_at=credential.expires_at,
        )


class IssueTokenRequest(BaseModel):
    label: str = ""


class IssuedTokenResponse(TokenOut):
    """`TokenOut` plus the raw token — visible exactly once, at issuance, and never
    recoverable afterward. Same discipline as `auth.bootstrap`'s bootstrap-token
    banner and `POST /auth/sudo`'s grant token.
    """

    token: str


def issue_token(
    db: Database,
    user_id: str,
    label: str,
    *,
    ttl_seconds: float | None = None,
    clock: Clock = utc_now,
) -> IssuedTokenResponse:
    """Mint a fresh token for `user_id`. Refuses (`BootstrapIdentityError`, 403) if
    `user_id` is the synthetic bootstrap identity — issue #28's invariant A.

    `ttl_seconds` (issue #39) is `None` by default — the token never expires, the
    existing behavior for every M2/M4-era call path (`POST /auth/me/tokens`,
    `POST /auth/users/{id}/tokens`, and `auth.bootstrap`'s own direct
    `create_api_token` calls, none of which pass it). A positive value stores
    `expires_at = clock() + ttl_seconds`, via the same injectable clock
    `auth.sudo.SudoService` and `ApiTokenAuthenticator` already use so the exact
    instant is assertable in a test rather than merely approximated. `ttl_seconds <=
    0` raises `ValueError` — mirrors `SudoSettings.ttl_seconds`'s own `gt=0` bound —
    since a zero or negative TTL would mint a token already expired at issuance,
    never honestly "short-lived."
    """
    assert_not_bootstrap_identity(db, user_id)
    if ttl_seconds is not None and ttl_seconds <= 0:
        raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds!r}")
    expires_at = (
        None if ttl_seconds is None else iso(clock() + timedelta(seconds=ttl_seconds))
    )
    token = secrets.token_urlsafe(_GENERATED_TOKEN_BYTES)
    register_secret(token)
    credential = CredentialRepository(db).create_api_token(
        user_id, token, label=label, expires_at=expires_at
    )
    return IssuedTokenResponse(
        **TokenOut.from_model(credential).model_dump(), token=token
    )


def list_tokens(db: Database, user_id: str) -> list[TokenOut]:
    return [
        TokenOut.from_model(credential)
        for credential in CredentialRepository(db).list_api_tokens(user_id)
    ]


def revoke_token(db: Database, user_id: str, credential_id: str) -> None:
    """Revoke one of `user_id`'s tokens.

    Refuses (`BootstrapIdentityError`, 403) for the bootstrap identity — issue #28's
    invariant A. Raises `NotFoundError` (404) when `credential_id` doesn't exist or
    belongs to a different user — `CredentialRepository.delete_api_token`'s own
    `user_id` scoping is what makes "belongs to a different user" indistinguishable
    from "doesn't exist" here, deliberately: nothing about the response should tell a
    caller whether a token id exists at all under someone else's account.
    """
    assert_not_bootstrap_identity(db, user_id)
    deleted = CredentialRepository(db).delete_api_token(credential_id, user_id)
    if not deleted:
        raise NotFoundError(
            f"no token {credential_id!r} for user {user_id!r}",
            details={"user_id": user_id, "token_id": credential_id},
        )
