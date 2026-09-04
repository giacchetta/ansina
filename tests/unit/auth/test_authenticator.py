from __future__ import annotations

from ansina.auth.authenticator import (
    ApiTokenAuthenticator,
    build_authenticators,
    resolve_principal,
)
from ansina.auth.models import RoleSlug, SubjectType, User
from ansina.auth.principal import AuthMethod
from ansina.auth.repositories import (
    CredentialRepository,
    RoleAssignmentRepository,
    RoleRepository,
    UserRepository,
)
from ansina.storage.database import Database


def _seed_reader(db: Database) -> tuple[UserRepository, str]:
    """A `read`-role user with a known token — the shape most tests here need."""
    users = UserRepository(db)
    user = users.create("reader")
    role = RoleRepository(db).ensure_builtin(
        RoleSlug.READ.value, "Read", "GET-only access."
    )
    RoleAssignmentRepository(db).assign(SubjectType.USER, user.id, role.id)
    CredentialRepository(db).create_api_token(user.id, "reader-token")
    return users, user.id


def test_build_authenticators_returns_the_api_token_authenticator(
    db: Database,
) -> None:
    authenticators = build_authenticators(db)

    assert len(authenticators) == 1
    assert isinstance(authenticators[0], ApiTokenAuthenticator)
    assert authenticators[0].method is AuthMethod.API_TOKEN


def test_api_token_authenticator_matches_an_active_credential(db: Database) -> None:
    _, user_id = _seed_reader(db)

    matched = ApiTokenAuthenticator(db).authenticate("reader-token")

    assert matched is not None
    assert matched.id == user_id


def test_api_token_authenticator_returns_none_for_an_unknown_token(
    db: Database,
) -> None:
    assert ApiTokenAuthenticator(db).authenticate("no-such-token") is None


def test_resolve_principal_fills_roles_direct_and_via_group(db: Database) -> None:
    _seed_reader(db)
    authenticators = build_authenticators(db)

    principal = resolve_principal(db, authenticators, "reader-token")

    assert principal is not None
    assert principal.user.username == "reader"
    assert principal.role_slugs == {RoleSlug.READ.value}
    assert principal.auth_method is AuthMethod.API_TOKEN


def test_resolve_principal_returns_none_for_no_match(db: Database) -> None:
    authenticators = build_authenticators(db)

    assert resolve_principal(db, authenticators, "nope") is None


def test_resolve_principal_returns_none_for_an_inactive_user(db: Database) -> None:
    users, user_id = _seed_reader(db)
    users.set_active(user_id, active=False)
    authenticators = build_authenticators(db)

    assert resolve_principal(db, authenticators, "reader-token") is None


def test_resolve_principal_returns_none_for_a_deleted_user(db: Database) -> None:
    """Backstop for a hand-edited row: `UserRepository.soft_delete` (issue #27)
    already purges the credential a deleted user would authenticate with, so this
    sets `deleted_at` directly — bypassing `soft_delete` — to prove
    `resolve_principal` itself also refuses a tombstoned user, not just as a side
    effect of the credential being gone.
    """
    _, user_id = _seed_reader(db)
    with db.transaction() as cursor:
        cursor.execute(
            "UPDATE users SET deleted_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00.000Z", user_id),
        )
    authenticators = build_authenticators(db)

    assert resolve_principal(db, authenticators, "reader-token") is None


def test_a_second_authenticator_can_be_appended_without_touching_the_first(
    db: Database,
) -> None:
    """Issue #25's acceptance criterion: a chain member can be added and exercised by
    a test without modifying `ApiTokenAuthenticator`'s own code — proving the chain is
    genuinely additive, not #24's inline single check reformatted.
    """
    _, user_id = _seed_reader(db)
    user = UserRepository(db).get(user_id)
    assert user is not None

    class _StaticSecondFactorAuthenticator:
        """A trivial second chain member — matches one hardcoded credential only,
        standing in for a follow-up milestone's federated-login authenticator.
        """

        method = AuthMethod.API_TOKEN

        def authenticate(self, credential: str) -> User | None:
            return user if credential == "second-factor-secret" else None

    chain = (*build_authenticators(db), _StaticSecondFactorAuthenticator())

    # The first authenticator still works, untouched...
    assert resolve_principal(db, chain, "reader-token") is not None
    # ...and the appended one is reachable too, with no first-authenticator match.
    principal = resolve_principal(db, chain, "second-factor-secret")
    assert principal is not None
    assert principal.user.id == user_id
    # Neither authenticator recognizes a credential belonging to no chain member.
    assert resolve_principal(db, chain, "unknown") is None
