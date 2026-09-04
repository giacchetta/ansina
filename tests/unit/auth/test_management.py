from __future__ import annotations

import pytest

from ansina.auth.management import (
    LastAdminError,
    SelfEscalationError,
    assert_admin_remains,
    assert_may_assign_role,
)
from ansina.auth.models import RoleSlug, SubjectType, User, Verb
from ansina.auth.principal import Principal
from ansina.auth.repositories import (
    ResourceRepository,
    RoleAssignmentRepository,
    RolePermissionRepository,
    RoleRepository,
    UserRepository,
)
from ansina.storage.database import Database

_CALLER = User(
    id="caller-1",
    username="caller",
    display_name="",
    active=True,
    created_at="2026-01-01T00:00:00Z",
)


def _seed_role(db: Database, slug: str, resource: str, verbs: tuple[Verb, ...]) -> str:
    ResourceRepository(db).upsert(resource, "")
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
