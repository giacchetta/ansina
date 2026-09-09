"""The `Authenticator` chain — formalizes issue #24's already-working DB-backed
`credentials` lookup behind a real `Protocol`, per issue #25's design note.

`ApiTokenAuthenticator` wraps `CredentialRepository.find_api_token_credential` (issue
#24, refactored by #28 to return the credential row rather than just the user) rather
than reimplementing the scan: this module's job is pluggability, not verification
logic. A follow-up milestone's OIDC authenticator becomes a second chain member here,
appended by `build_authenticators`, with zero changes to `ApiTokenAuthenticator` or to
`resolve_principal` — the property issue #25's acceptance criteria calls out
explicitly.

Issue #28 adds coalesced `last_used_at` bookkeeping to `ApiTokenAuthenticator.
authenticate`: `find_user_by_api_token` (and, before it, `find_api_token_credential`)
already full-scans `credentials` on every authenticated request, so a write on every
one of those requests would additionally serialize SQLite writers against the tick
loop and any concurrent reader. The write instead fires only when the stored
`last_used_at` is older than `last_used_resolution_seconds` — at most once per token
per interval — via an injectable `clock` so the staleness threshold is testable
without real sleeping, the same pattern `auth.sudo.SudoService` already uses.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Protocol

from ansina.auth.clock import Clock, iso, utc_now
from ansina.auth.models import User
from ansina.auth.principal import AuthMethod, Principal
from ansina.auth.repositories import CredentialRepository, RoleAssignmentRepository
from ansina.storage.database import Database

if TYPE_CHECKING:
    from ansina.config.settings import Settings


class Authenticator(Protocol):
    """One credential-verification strategy in the chain. `method` identifies which
    `AuthMethod` a successful match should be recorded as; `authenticate` returns the
    matched `User`, or `None` if this authenticator doesn't recognize the credential —
    never raises for "no match," only for a genuine failure of the lookup itself.
    """

    @property
    def method(self) -> AuthMethod: ...

    def authenticate(self, credential: str) -> User | None: ...


class ApiTokenAuthenticator:
    """Matches `credential` against any active `api_token` row via
    `CredentialRepository.find_api_token_credential`, then (issue #28) coalesces a
    `last_used_at` touch onto the matched row before returning its owning user.
    """

    method = AuthMethod.API_TOKEN

    def __init__(
        self,
        db: Database,
        *,
        last_used_resolution_seconds: float,
        clock: Clock = utc_now,
    ) -> None:
        self._db = db
        self._credentials = CredentialRepository(db)
        self._resolution_seconds = last_used_resolution_seconds
        self._clock = clock

    def authenticate(self, credential: str) -> User | None:
        matched = self._credentials.find_api_token_credential(credential)
        if matched is None:
            return None
        self._touch_if_stale(matched.id, matched.last_used_at)
        user_row = (
            self._db.connection()
            .execute("SELECT * FROM users WHERE id = ?", (matched.user_id,))
            .fetchone()
        )
        return User.from_row(user_row) if user_row is not None else None

    def _touch_if_stale(self, credential_id: str, last_used_at: str | None) -> None:
        """A **string** comparison against `now - resolution`, not a parse of
        `last_used_at` — millisecond-precision ISO 8601 UTC (`auth.clock.iso`) sorts
        identically as text, the same property `SudoGrantRepository.find_active`'s
        `expires_at > ?` comparison already relies on. That avoids a parse-failure
        path to cover, and keeps this check as cheap as the write it's guarding.
        """
        threshold = iso(self._clock() - timedelta(seconds=self._resolution_seconds))
        if last_used_at is None or last_used_at < threshold:
            self._credentials.touch_last_used(credential_id, now=iso(self._clock()))


def build_authenticators(db: Database, settings: Settings) -> tuple[Authenticator, ...]:
    """The chain `BearerAuthMiddleware` walks, in order. A follow-up milestone appends
    to this tuple; nothing else about the chain's callers changes. Takes `settings`
    (issue #28, not just `db`) since `ApiTokenAuthenticator` needs
    `[security] token_last_used_resolution_seconds`.
    """
    return (
        ApiTokenAuthenticator(
            db,
            last_used_resolution_seconds=(
                settings.security.token_last_used_resolution_seconds
            ),
        ),
    )


def resolve_principal(
    db: Database, authenticators: tuple[Authenticator, ...], credential: str
) -> Principal | None:
    """Walk `authenticators` in order, returning the first match's `Principal`, or
    `None` if none of them recognize `credential` or the matched user is inactive.

    An inactive user's token deliberately stops authenticating here — neither
    `find_api_token_credential` nor `find_user_by_api_token` filters on `users.active`
    (issue #24 never asked either to), so this is the one place that invariant is
    enforced.
    """
    for authenticator in authenticators:
        user = authenticator.authenticate(credential)
        if user is None:
            continue
        if not user.active or user.deleted_at is not None:
            # `deleted_at` (issue #27) is a one-way tombstone distinct from `active`'s
            # suspend/resume flag — `UserRepository.soft_delete` already purges every
            # credential a deleted user could authenticate with, but this check is the
            # backstop against a hand-edited row (see the migration's own docstring).
            return None
        roles = RoleAssignmentRepository(db).roles_for_user(user.id)
        return Principal(
            user=user,
            role_ids=frozenset(role.id for role in roles),
            role_slugs=frozenset(role.slug for role in roles),
            auth_method=authenticator.method,
        )
    return None
