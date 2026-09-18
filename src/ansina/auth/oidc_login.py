"""`OidcLoginService` — the OIDC login exchange. See issue #43.

M3's open consideration #5 ("`Authenticator` is the wrong shape for OIDC") is resolved
here by *not* using it: this is a **login exchange**, not a chain member. The
authorization-code flow runs once per login (`start_login` / `complete_login`),
validates the ID token (`ansina.auth.oidc.validate_id_token`), JIT-provisions/links the
Ansina user, and refreshes its `role_mappings`-derived roles (`ansina.auth.role_sync
.sync_mapped_roles`, issue #42's first caller). Every later request stays on the
existing `ApiTokenAuthenticator` — `Authenticator`, `BearerAuthMiddleware`,
`resolve_principal`, and the entire `tui/` client are untouched by this issue.

`complete_login`'s five steps run in one fixed order, and that order is the whole
point: every possible rejection (unknown/expired/replayed state, a token that fails
signature/issuer/audience/expiry/nonce validation) happens *before* step 4
(provisioning) ever runs — issue #43's AC #2 requires exactly this, so a malformed or
forged callback can never create or modify a user, an identity, or a role assignment.
`complete_login` returns the provisioned `User`, not a minted token: minting one
(`ansina.api.tokens.issue_token`, issue #39's first HTTP-reachable caller) is left to
`api.routes.oidc`, deliberately — `ansina.auth` (this package) never imports from
`ansina.api` anywhere else in the codebase, and `ansina.api`'s own package `__init__`
imports `create_app`, which imports back from `ansina.auth`; a domain module reaching
into `ansina.api.tokens` here would make that a circular import. `clock`/
`token_ttl_seconds` are exposed as read-only properties so the route can mint with the
exact same injected clock this service used to validate the login, rather than a
second, independently-resolved one.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlencode

from ansina.auth.bootstrap import is_bootstrap_identity
from ansina.auth.clock import Clock, iso, utc_now
from ansina.auth.oidc import (
    HttpxOidcClient,
    OidcHttpClient,
    OidcTokenError,
    code_challenge,
    discover,
    exchange_code,
    fetch_jwks,
    new_code_verifier,
    validate_id_token,
)
from ansina.auth.repositories import (
    ExternalIdentityRepository,
    OidcLoginStateRepository,
    UserRepository,
)
from ansina.auth.role_sync import sync_mapped_roles
from ansina.errors import AuthError
from ansina.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ansina.auth.models import User
    from ansina.config.settings import OidcSettings, Settings
    from ansina.storage.database import Database

logger = get_logger(__name__)

# `state`/`nonce` don't need PKCE's slightly larger 64-byte floor (see `auth.oidc
# ._CODE_VERIFIER_BYTES`'s own comment) — 32 raw bytes matches every other
# high-entropy token this codebase mints (`auth.bootstrap`'s bootstrap token,
# `api.tokens.issue_token`'s minted tokens).
_STATE_BYTES = 32
_NONCE_BYTES = 32


class OidcStateError(AuthError):
    """The `state` presented to `GET /auth/oidc/callback` doesn't match any row in
    `oidc_login_states` — never issued, already expired, or already redeemed
    (`OidcLoginStateRepository.take` deletes a row the moment it's read, so a replayed
    `state` lands here too, indistinguishable from one that never existed).
    """

    code: ClassVar[str] = "ansina.auth.oidc_state_invalid"


class OidcCallbackError(AuthError):
    """The callback request itself is malformed: the identity provider redirected
    back with an `error` query parameter (e.g. `access_denied` — the user declined
    consent), or is missing `code`/`state` entirely. Raised before `OidcLoginService`
    is even consulted — see `api.routes.oidc`.
    """

    code: ClassVar[str] = "ansina.auth.oidc_callback_failed"


class OidcProvisioningError(AuthError):
    """A successfully-validated login was refused at the user-provisioning step: its
    linked user no longer exists, is tombstoned, is deactivated, or would have to be
    linked onto the bootstrap identity. A verified ID token proves *who the IdP says
    logged in*, not that Ansina is willing to let that identity act as any given
    account.
    """

    code: ClassVar[str] = "ansina.auth.oidc_provisioning_refused"


@dataclass(frozen=True, slots=True)
class LoginStart:
    """`OidcLoginService.start_login()`'s result — what `POST /auth/oidc/login`
    returns to the caller so it can redirect (or hand the URL to) the resource owner.
    """

    authorization_url: str
    state: str
    expires_at: str


def _derive_username(claims: Mapping[str, Any], subject: str) -> str:
    """The first non-empty string among `preferred_username`, `email`, and `sub` —
    tried in that order since `preferred_username` is the OIDC-standard "the user's
    own chosen handle" claim, `email` is the next most human-readable fallback most
    IdPs always send, and `sub` (always present, already validated as a non-empty
    string by the time this is called) never fails to produce a candidate.
    """
    for key in ("preferred_username", "email"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value
    return subject


class OidcLoginService:
    """Orchestrates one login end to end. Constructed once per process by
    `build_oidc_login_service` (`None` when `[security.oidc] enabled = false`) and
    handed to `api.routes.oidc` via `app.state.oidc`, the same "construct once,
    inject, `None` when off" shape `app.state.heart`/`app.state.brain` already use.
    """

    def __init__(
        self,
        db: Database,
        oidc_settings: OidcSettings,
        *,
        client: OidcHttpClient,
        clock: Clock = utc_now,
    ) -> None:
        self._db = db
        self._oidc = oidc_settings
        self._client = client
        self._clock = clock
        self._states = OidcLoginStateRepository(db)
        self._identities = ExternalIdentityRepository(db)
        self._users = UserRepository(db)

    @property
    def clock(self) -> Clock:
        """The injected clock this service validated the login against — `api.routes
        .oidc` mints the resulting `api_token` with this same clock, rather than a
        second, independently-resolved `utc_now()`.
        """
        return self._clock

    @property
    def token_ttl_seconds(self) -> float:
        """`[security.oidc] token_ttl_seconds` — how long the `api_token` minted at
        the end of a successful login should stay valid. Exposed here so the route
        layer's `issue_token(..., ttl_seconds=oidc.token_ttl_seconds)` call reads it
        from the same resolved settings this service already holds.
        """
        return self._oidc.token_ttl_seconds

    def start_login(self) -> LoginStart:
        """Discover the IdP's authorization endpoint, mint `state`/`nonce`/PKCE
        `code_verifier`, persist them, and build the URL the caller redirects the
        resource owner to. Also sweeps any of this service's own previously-expired,
        never-redeemed rows (`OidcLoginStateRepository.delete_expired`) — piggybacking
        the cleanup on the one operation that's guaranteed to run periodically, rather
        than a dedicated background task for what's expected to be a handful of rows.
        """
        now = self._clock()
        self._states.delete_expired(now=iso(now))

        metadata = discover(self._client, self._oidc.issuer)

        state = secrets.token_urlsafe(_STATE_BYTES)
        nonce = secrets.token_urlsafe(_NONCE_BYTES)
        verifier = new_code_verifier()
        expires_at = iso(now + timedelta(seconds=self._oidc.state_ttl_seconds))
        self._states.create(state, nonce, verifier, expires_at=expires_at)

        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._oidc.client_id,
                "redirect_uri": self._oidc.redirect_uri,
                "scope": " ".join(self._oidc.scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge(verifier),
                "code_challenge_method": "S256",
            }
        )
        authorization_url = f"{metadata.authorization_endpoint}?{query}"
        return LoginStart(
            authorization_url=authorization_url, state=state, expires_at=expires_at
        )

    def complete_login(self, code: str, state: str) -> User:
        """The five-step exchange described in this module's docstring, returning the
        provisioned `User` — minting an `api_token` for it is the caller's job (see
        the module docstring for why). Every step before "4. provision" can only ever
        reject — nothing before it touches `users`, `external_identities`, or
        `role_assignments`.
        """
        # 1. Single-use state lookup — CSRF protection for the redirect, and the
        # source of this login's own nonce/code_verifier.
        login_state = self._states.take(state, now=iso(self._clock()))
        if login_state is None:
            raise OidcStateError(
                "unknown, expired, or already-used state — restart the login"
            )

        metadata = discover(self._client, self._oidc.issuer)

        # 2. Redeem the authorization code — proves the caller actually completed the
        # IdP's login/consent screen, and (via PKCE) that this process is the one
        # that started it.
        client_secret = self._oidc.client_secret
        # Guaranteed non-None: `OidcSettings._validate_enabled_requires_credentials`
        # refuses to construct a `Settings` with `enabled=True` and no
        # `client_secret`, and `build_oidc_login_service` only ever builds this
        # service when `enabled` is true.
        assert client_secret is not None  # unreachable — see above
        id_token = exchange_code(
            self._client,
            metadata,
            client_id=self._oidc.client_id,
            client_secret=client_secret.get_secret_value(),
            redirect_uri=self._oidc.redirect_uri,
            code=code,
            code_verifier=login_state.code_verifier,
        )

        # 3. Validate the ID token: signature, issuer, audience, expiry, and nonce —
        # see `ansina.auth.oidc.validate_id_token`'s own docstring for the full list.
        jwks = fetch_jwks(self._client, metadata.jwks_uri)
        claims = validate_id_token(
            id_token,
            jwks,
            issuer=self._oidc.issuer,
            audience=self._oidc.client_id,
            nonce=login_state.nonce,
        )
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            # `validate_id_token` already requires `sub` via `options={"require":
            # [...]}` — a defensive, typed narrowing rather than an `assert`, since
            # claims are IdP-controlled data, not a call this codebase controls both
            # ends of.
            raise OidcTokenError(
                "id_token carries no usable 'sub' claim"
            )  # unreachable

        # 4. Provision or link the Ansina user this identity resolves to.
        user = self._provision_user(subject, claims)

        # 5. Refresh this provider's role mappings against the login's current
        # claims — issue #42's `sync_mapped_roles`, called on *every* login (not only
        # the first), scoped entirely to `source=self._oidc.issuer` so a manually
        # (`local`) assigned role, or one from a different provider, is never touched.
        sync_mapped_roles(self._db, user.id, self._oidc.issuer, claims)

        return user

    def _provision_user(self, subject: str, claims: Mapping[str, Any]) -> User:
        """Resolve `claims` to an Ansina `User`: reuse an existing `(provider,
        subject)` link, link onto a matching local username, or create a fresh
        federated user — in that order. See this module's docstring and issue #43's
        planning notes for why "link onto a matching username" was chosen over
        refusing or auto-disambiguating: it's what lets an operator migrate an
        existing local user onto the IdP without a separate migration step. The three
        refusals below (tombstoned, deactivated, bootstrap identity) are what keep
        that convenience from ever reviving access an Admin deliberately removed.
        """
        issuer = self._oidc.issuer
        identity = self._identities.get_by_provider_subject(issuer, subject)
        if identity is not None:
            user = self._users.get(identity.user_id)
            if user is None or user.deleted_at is not None:
                raise OidcProvisioningError(
                    "this identity's linked user no longer exists"
                )
            if not user.active:
                raise OidcProvisioningError(
                    f"user {user.username!r} is deactivated — an Admin must "
                    "reactivate it before this identity can log in again"
                )
            return user

        username = _derive_username(claims, subject)
        existing = self._users.get_by_username(username)
        if existing is not None:
            if existing.deleted_at is not None:
                raise OidcProvisioningError(
                    f"a deleted user already holds the username {username!r} — "
                    "this identity cannot be linked to it"
                )
            if not existing.active:
                raise OidcProvisioningError(
                    f"user {username!r} is deactivated — an Admin must reactivate "
                    "it before this identity can be linked to it"
                )
            if is_bootstrap_identity(self._db, existing.id):
                raise OidcProvisioningError(
                    "the bootstrap identity cannot be linked to an external identity"
                )
            # A username match links the IdP subject to a *pre-existing* account and
            # confers that account's roles on it — deliberate (see this module's
            # docstring), but worth a loud audit trail: anyone who can make the IdP
            # assert this exact preferred_username/email inherits this account.
            logger.warning(
                "oidc login: linking external identity (provider=%r, subject=%r) "
                "to existing local user %r (id=%r) on a username match — that "
                "identity now inherits this user's roles",
                issuer,
                subject,
                existing.username,
                existing.id,
            )
            self._identities.create(existing.id, issuer, subject)
            return existing

        display_name = claims.get("name")
        user = self._users.create(
            username,
            display_name=display_name if isinstance(display_name, str) else "",
            local_identity=False,
        )
        self._identities.create(user.id, issuer, subject)
        return user


def build_oidc_login_service(
    db: Database,
    settings: Settings,
    *,
    client: OidcHttpClient | None = None,
    clock: Clock = utc_now,
) -> OidcLoginService | None:
    """The default `OidcLoginService` factory. Returns `None` when `[security.oidc]
    enabled = false` — the same "`None` when off" shape `heart`/`brain` already use in
    `create_app`, so `app.state.oidc is None` is what `api.routes.oidc` checks to
    answer 503. `client` defaults to a real `HttpxOidcClient`; tests pass a fake.
    """
    oidc_settings = settings.security.oidc
    if not oidc_settings.enabled:
        return None
    resolved_client = client or HttpxOidcClient(
        timeout_seconds=oidc_settings.http_timeout_seconds
    )
    return OidcLoginService(db, oidc_settings, client=resolved_client, clock=clock)
