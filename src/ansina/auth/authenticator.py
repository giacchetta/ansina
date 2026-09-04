"""The `Authenticator` chain — formalizes issue #24's already-working DB-backed
`credentials` lookup behind a real `Protocol`, per issue #25's design note.

`ApiTokenAuthenticator` wraps `CredentialRepository.find_user_by_api_token` (issue #24)
rather than reimplementing it: this issue's job is pluggability, not new verification
logic. A follow-up milestone's OIDC authenticator becomes a second chain member here,
appended by `build_authenticators`, with zero changes to `ApiTokenAuthenticator` or to
`resolve_principal` — the property issue #25's acceptance criteria calls out explicitly.
"""

from __future__ import annotations

from typing import Protocol

from ansina.auth.models import User
from ansina.auth.principal import AuthMethod, Principal
from ansina.auth.repositories import CredentialRepository, RoleAssignmentRepository
from ansina.storage.database import Database


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
    """The one authenticator issue #25 ships: matches `credential` against any active
    `api_token` row via `CredentialRepository.find_user_by_api_token`.
    """

    method = AuthMethod.API_TOKEN

    def __init__(self, db: Database) -> None:
        self._credentials = CredentialRepository(db)

    def authenticate(self, credential: str) -> User | None:
        return self._credentials.find_user_by_api_token(credential)


def build_authenticators(db: Database) -> tuple[Authenticator, ...]:
    """The chain `BearerAuthMiddleware` walks, in order. A follow-up milestone appends
    to this tuple; nothing else about the chain's callers changes.
    """
    return (ApiTokenAuthenticator(db),)


def resolve_principal(
    db: Database, authenticators: tuple[Authenticator, ...], credential: str
) -> Principal | None:
    """Walk `authenticators` in order, returning the first match's `Principal`, or
    `None` if none of them recognize `credential` or the matched user is inactive.

    An inactive user's token deliberately stops authenticating here — `find_user_by_
    api_token` itself doesn't filter on `users.active` (issue #24 never asked it to),
    so this is the one place that invariant is enforced.
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
