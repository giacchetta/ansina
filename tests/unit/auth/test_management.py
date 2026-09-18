from __future__ import annotations

from pathlib import Path

import pytest

from ansina.auth.bootstrap import ensure_bootstrap_admin
from ansina.auth.management import (
    BootstrapIdentityError,
    InvalidGrantError,
    LastAdminError,
    SelfEscalationError,
    TokenAlreadyIssuedError,
    TotpAlreadyEnrolledError,
    assert_admin_remains,
    assert_grants_grantable,
    assert_may_assign_role,
    assert_may_grant_permissions,
    assert_no_existing_api_token,
    assert_not_bootstrap_identity,
    assert_totp_not_enrolled,
)
from ansina.auth.models import CredentialType, RoleSlug, SubjectType, User, Verb
from ansina.auth.principal import Principal
from ansina.auth.reconciler import reconcile_builtin_roles
from ansina.auth.repositories import (
    CredentialRepository,
    ResourceRepository,
    RoleAssignmentRepository,
    RolePermissionRepository,
    RoleRepository,
    UserRepository,
)
from ansina.config import load_settings
from ansina.storage.database import Database

_CALLER = User(
    id="caller-1",
    username="caller",
    display_name="",
    active=True,
    created_at="2026-01-01T00:00:00Z",
)


def _seed_role(db: Database, slug: str, resource: str, verbs: tuple[Verb, ...]) -> str:
    ResourceRepository(db).upsert(resource, "", verbs=frozenset())
    role = RoleRepository(db).ensure_builtin(slug, slug.title(), "")
    for verb in verbs:
        RolePermissionRepository(db).grant(role.id, resource, verb)
    return role.id


# --- assert_may_assign_role -----------------------------------------------------------


def test_non_admin_cannot_assign_the_admin_role(db: Database) -> None:
    admin_role = RoleRepository(db).ensure_builtin("admin", "Admin", "")
    principal = Principal(
        user=_CALLER,
        role_ids=frozenset(),
        role_slugs=frozenset({RoleSlug.MAINTAIN.value}),
    )

    with pytest.raises(SelfEscalationError) as excinfo:
        assert_may_assign_role(db, principal, admin_role)

    assert excinfo.value.code == "ansina.auth.self_escalation"


def test_admin_can_assign_the_admin_role(db: Database) -> None:
    admin_role = RoleRepository(db).ensure_builtin("admin", "Admin", "")
    principal = Principal(
        user=_CALLER,
        role_ids=frozenset(),
        role_slugs=frozenset({RoleSlug.ADMIN.value}),
    )

    assert_may_assign_role(db, principal, admin_role)  # must not raise


def test_non_admin_cannot_assign_a_role_granting_an_auth_resource(
    db: Database,
) -> None:
    """A sudo'd Maintain holds the same `role_permissions` rows as Admin under M2's
    fixed policy, so this must be refused directly — the subset check alone would not
    catch it (see `assert_may_assign_role`'s own docstring).
    """
    maintain_role_id = _seed_role(
        db, "maintain", "auth.users", (Verb.GET, Verb.POST, Verb.DELETE)
    )
    target_role_id = _seed_role(db, "custom-auth-role", "auth.users", (Verb.GET,))
    target_role = RoleRepository(db).get(target_role_id)
    assert target_role is not None
    principal = Principal(
        user=_CALLER,
        role_ids=frozenset({maintain_role_id}),
        role_slugs=frozenset({RoleSlug.MAINTAIN.value}),
    )

    with pytest.raises(SelfEscalationError):
        assert_may_assign_role(db, principal, target_role)


def test_admin_can_assign_a_role_granting_an_auth_resource(db: Database) -> None:
    admin_role_id = _seed_role(db, "admin", "auth.users", (Verb.GET, Verb.DELETE))
    target_role_id = _seed_role(db, "custom-auth-role", "auth.users", (Verb.GET,))
    target_role = RoleRepository(db).get(target_role_id)
    assert target_role is not None
    principal = Principal(
        user=_CALLER,
        role_ids=frozenset({admin_role_id}),
        role_slugs=frozenset({RoleSlug.ADMIN.value}),
    )

    assert_may_assign_role(db, principal, target_role)  # must not raise


def test_cannot_assign_a_role_with_grants_the_caller_lacks(db: Database) -> None:
    caller_role_id = _seed_role(db, "read", "heart.tick", (Verb.GET,))
    target_role_id = _seed_role(
        db, "elevated", "heart.tick", (Verb.GET, Verb.POST, Verb.DELETE)
    )
    target_role = RoleRepository(db).get(target_role_id)
    assert target_role is not None
    principal = Principal(
        user=_CALLER,
        role_ids=frozenset({caller_role_id}),
        role_slugs=frozenset({RoleSlug.READ.value}),
    )

    with pytest.raises(SelfEscalationError) as excinfo:
        assert_may_assign_role(db, principal, target_role)

    assert "elevated" in excinfo.value.details["role"]
    assert excinfo.value.details["excess"]


def test_can_assign_a_role_whose_grants_are_a_subset_of_the_callers(
    db: Database,
) -> None:
    caller_role_id = _seed_role(
        db, "write", "heart.tick", (Verb.GET, Verb.POST, Verb.PATCH)
    )
    target_role_id = _seed_role(db, "read", "heart.tick", (Verb.GET,))
    target_role = RoleRepository(db).get(target_role_id)
    assert target_role is not None
    principal = Principal(
        user=_CALLER,
        role_ids=frozenset({caller_role_id}),
        role_slugs=frozenset({RoleSlug.WRITE.value}),
    )

    assert_may_assign_role(db, principal, target_role)  # must not raise


# --- assert_may_grant_permissions (issue #40, generalizing the checks above over a ----
# --- raw grant set rather than an existing role's, so POST/PATCH /auth/roles can run --
# --- them at grant-edit time, not only at role-assignment time) -----------------------


def test_non_admin_cannot_grant_an_auth_resource(db: Database) -> None:
    ResourceRepository(db).upsert("auth.roles", "", verbs=frozenset({Verb.GET}))
    principal = Principal(
        user=_CALLER,
        role_ids=frozenset(),
        role_slugs=frozenset({RoleSlug.MAINTAIN.value}),
    )

    with pytest.raises(SelfEscalationError) as excinfo:
        assert_may_grant_permissions(
            db, principal, frozenset({("auth.roles", Verb.GET)})
        )

    assert excinfo.value.code == "ansina.auth.self_escalation"


def test_admin_can_grant_an_auth_resource(db: Database) -> None:
    admin_role_id = _seed_role(db, "admin", "auth.roles", (Verb.GET,))
    principal = Principal(
        user=_CALLER,
        role_ids=frozenset({admin_role_id}),
        role_slugs=frozenset({RoleSlug.ADMIN.value}),
    )

    assert_may_grant_permissions(
        db, principal, frozenset({("auth.roles", Verb.GET)})
    )  # must not raise


def test_cannot_grant_permissions_the_caller_lacks(db: Database) -> None:
    caller_role_id = _seed_role(db, "read", "heart.tick", (Verb.GET,))
    principal = Principal(
        user=_CALLER,
        role_ids=frozenset({caller_role_id}),
        role_slugs=frozenset({RoleSlug.READ.value}),
    )

    with pytest.raises(SelfEscalationError) as excinfo:
        assert_may_grant_permissions(
            db, principal, frozenset({("heart.tick", Verb.POST)})
        )

    assert excinfo.value.details["excess"]


def test_can_grant_a_subset_of_the_callers_own_permissions(db: Database) -> None:
    caller_role_id = _seed_role(
        db, "write", "heart.tick", (Verb.GET, Verb.POST, Verb.PATCH)
    )
    principal = Principal(
        user=_CALLER,
        role_ids=frozenset({caller_role_id}),
        role_slugs=frozenset({RoleSlug.WRITE.value}),
    )

    assert_may_grant_permissions(
        db, principal, frozenset({("heart.tick", Verb.GET)})
    )  # must not raise


# --- assert_grants_grantable (issue #40) -------------------------------------------


def test_refuses_an_uncatalogued_resource(db: Database) -> None:
    with pytest.raises(InvalidGrantError) as excinfo:
        assert_grants_grantable(db, frozenset({("no.such.resource", Verb.GET)}))

    assert excinfo.value.code == "ansina.auth.invalid_grant"
    assert "not a catalogued resource" in excinfo.value.details["invalid"][0]


def test_refuses_a_non_grantable_self_resource(db: Database) -> None:
    ResourceRepository(db).upsert("me.profile", "", verbs=frozenset({Verb.GET}))

    with pytest.raises(InvalidGrantError) as excinfo:
        assert_grants_grantable(db, frozenset({("me.profile", Verb.GET)}))

    assert "not grantable" in excinfo.value.details["invalid"][0]


def test_refuses_a_verb_the_resource_does_not_serve(db: Database) -> None:
    ResourceRepository(db).upsert("system.version", "", verbs=frozenset({Verb.GET}))

    with pytest.raises(InvalidGrantError) as excinfo:
        assert_grants_grantable(db, frozenset({("system.version", Verb.POST)}))

    assert "verb not served" in excinfo.value.details["invalid"][0]


def test_allows_a_served_grantable_grant(db: Database) -> None:
    ResourceRepository(db).upsert(
        "heart.tick", "", verbs=frozenset({Verb.GET, Verb.POST})
    )

    assert_grants_grantable(db, frozenset({("heart.tick", Verb.GET)}))  # must not raise


def test_allows_an_empty_grant_set(db: Database) -> None:
    assert_grants_grantable(db, frozenset())  # must not raise


# --- assert_admin_remains -------------------------------------------------------------


def _seed_user_with_role(db: Database, username: str, role_id: str) -> str:
    user = UserRepository(db).create(username)
    RoleAssignmentRepository(db).assign(SubjectType.USER, user.id, role_id)
    return user.id


def test_refuses_to_remove_the_sole_remaining_admin(db: Database) -> None:
    admin_role = RoleRepository(db).ensure_builtin("admin", "Admin", "")
    user_id = _seed_user_with_role(db, "sole-admin", admin_role.id)

    with pytest.raises(LastAdminError) as excinfo:
        assert_admin_remains(db, frozenset({user_id}))

    assert excinfo.value.code == "ansina.auth.last_admin"


def test_allows_removing_one_of_two_admins(db: Database) -> None:
    admin_role = RoleRepository(db).ensure_builtin("admin", "Admin", "")
    first = _seed_user_with_role(db, "admin-one", admin_role.id)
    _seed_user_with_role(db, "admin-two", admin_role.id)

    assert_admin_remains(db, frozenset({first}))  # must not raise


def test_removing_a_non_admin_never_raises(db: Database) -> None:
    admin_role = RoleRepository(db).ensure_builtin("admin", "Admin", "")
    _seed_user_with_role(db, "sole-admin", admin_role.id)
    other = UserRepository(db).create("plain-user")

    assert_admin_remains(db, frozenset({other.id}))  # must not raise


# --- assert_not_bootstrap_identity (issue #28's invariant A) --------------------------


def test_refuses_the_bootstrap_identity(
    db: Database, clean_env: None, tmp_cwd: Path
) -> None:
    reconcile_builtin_roles(db)
    ensure_bootstrap_admin(db, load_settings())
    bootstrap = UserRepository(db).get_by_username("bootstrap-admin")
    assert bootstrap is not None

    with pytest.raises(BootstrapIdentityError) as excinfo:
        assert_not_bootstrap_identity(db, bootstrap.id)

    assert excinfo.value.code == "ansina.auth.bootstrap_identity"


def test_allows_an_ordinary_user(db: Database) -> None:
    user = UserRepository(db).create("ordinary")

    assert_not_bootstrap_identity(db, user.id)  # must not raise


# --- assert_no_existing_api_token (issue #28's invariant B) ---------------------------


def test_refuses_a_user_who_already_holds_a_token(db: Database) -> None:
    user = UserRepository(db).create("alice")
    CredentialRepository(db).create_api_token(user.id, "already-has-one")

    with pytest.raises(TokenAlreadyIssuedError) as excinfo:
        assert_no_existing_api_token(db, user.id)

    assert excinfo.value.code == "ansina.auth.token_already_issued"


def test_allows_a_user_with_no_tokens_yet(db: Database) -> None:
    user = UserRepository(db).create("alice")

    assert_no_existing_api_token(db, user.id)  # must not raise


def test_allows_again_after_the_only_token_is_revoked(db: Database) -> None:
    """The recovery path: revoking a user's last token drops the count back to
    zero, so an Admin can issue a fresh one.
    """
    user = UserRepository(db).create("alice")
    credential = CredentialRepository(db).create_api_token(user.id, "the-one-token")
    CredentialRepository(db).delete_api_token(credential.id, user.id)

    assert_no_existing_api_token(db, user.id)  # must not raise


# --- assert_totp_not_enrolled (issue #41) ---------------------------------------------


def test_refuses_a_user_already_enrolled_in_totp(db: Database) -> None:
    user = UserRepository(db).create("alice")
    CredentialRepository(db).create_totp_secret(user.id, "v1:a:b")

    with pytest.raises(TotpAlreadyEnrolledError) as excinfo:
        assert_totp_not_enrolled(db, user.id)

    assert excinfo.value.code == "ansina.auth.totp_already_enrolled"


def test_allows_a_user_with_no_totp_enrollment_yet(db: Database) -> None:
    user = UserRepository(db).create("alice")

    assert_totp_not_enrolled(db, user.id)  # must not raise


def test_allows_again_after_the_totp_credential_is_disabled(db: Database) -> None:
    user = UserRepository(db).create("alice")
    CredentialRepository(db).create_totp_secret(user.id, "v1:a:b")
    CredentialRepository(db).delete_credentials(user.id, CredentialType.TOTP)

    assert_totp_not_enrolled(db, user.id)  # must not raise
