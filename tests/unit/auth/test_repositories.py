from __future__ import annotations

import pytest

from ansina.auth.hashing import Argon2Params
from ansina.auth.models import CredentialType, SubjectType, Verb
from ansina.auth.repositories import (
    BuiltinRoleError,
    CredentialRepository,
    DuplicateError,
    ExternalIdentityRepository,
    GroupRepository,
    ResourceRepository,
    RoleAssignmentRepository,
    RolePermissionRepository,
    RoleRepository,
    UnknownSubjectError,
    UserRepository,
)
from ansina.storage.database import Database

# --- ResourceRepository -----------------------------------------------------


def test_resource_upsert_creates_and_updates(db: Database) -> None:
    resources = ResourceRepository(db)

    created = resources.upsert("heart.tick", "first description")
    assert created.name == "heart.tick"
    assert created.description == "first description"

    updated = resources.upsert("heart.tick", "second description")
    assert updated.description == "second description"
    assert len(resources.list_all()) == 1


def test_resource_delete_cascades_role_permissions(db: Database) -> None:
    resources = ResourceRepository(db)
    roles = RoleRepository(db)
    permissions = RolePermissionRepository(db)
    resources.upsert("heart.tick", "")
    role = roles.create("read", "Read", "")
    permissions.grant(role.id, "heart.tick", Verb.GET)

    resources.delete("heart.tick")

    assert resources.list_all() == []
    assert permissions.list_for_role(role.id) == []


def test_resource_delete_of_unknown_name_is_a_no_op(db: Database) -> None:
    ResourceRepository(db).delete("does.not.exist")


# --- RoleRepository ----------------------------------------------------------


def test_role_create_and_get(db: Database) -> None:
    roles = RoleRepository(db)

    role = roles.create("read", "Read", "GET-only access")

    assert roles.get(role.id) == role
    assert roles.get_by_slug("read") == role
    assert role.builtin is False


def test_role_get_of_unknown_id_returns_none(db: Database) -> None:
    assert RoleRepository(db).get("no-such-id") is None
    assert RoleRepository(db).get_by_slug("no-such-slug") is None


def test_role_create_rejects_a_duplicate_slug(db: Database) -> None:
    roles = RoleRepository(db)
    roles.create("read", "Read", "")

    with pytest.raises(DuplicateError, match="read"):
        roles.create("read", "Read Again", "")


def test_role_ensure_builtin_creates_once_and_is_idempotent(db: Database) -> None:
    roles = RoleRepository(db)

    first = roles.ensure_builtin("admin", "Admin", "full access")
    second = roles.ensure_builtin("admin", "Admin", "full access")

    assert first == second
    assert first.builtin is True
    assert len(roles.list_all()) == 1


def test_role_delete_removes_a_non_builtin_role(db: Database) -> None:
    roles = RoleRepository(db)
    role = roles.create("custom", "Custom", "")

    roles.delete(role.id)

    assert roles.get(role.id) is None


def test_role_delete_of_unknown_id_is_a_no_op(db: Database) -> None:
    RoleRepository(db).delete("no-such-id")


def test_role_delete_refuses_a_builtin_role(db: Database) -> None:
    roles = RoleRepository(db)
    role = roles.ensure_builtin("admin", "Admin", "")

    with pytest.raises(BuiltinRoleError, match="admin"):
        roles.delete(role.id)

    assert roles.get(role.id) is not None


# --- RolePermissionRepository --------------------------------------------------


def test_role_permission_grant_and_list(db: Database) -> None:
    roles = RoleRepository(db)
    permissions = RolePermissionRepository(db)
    ResourceRepository(db).upsert("heart.tick", "")
    role = roles.create("read", "Read", "")

    permissions.grant(role.id, "heart.tick", Verb.GET)
    permissions.grant(role.id, "heart.tick", Verb.GET)  # idempotent

    grants = permissions.list_for_role(role.id)
    assert [(g.resource, g.verb) for g in grants] == [("heart.tick", Verb.GET)]


def test_role_permission_revoke(db: Database) -> None:
    roles = RoleRepository(db)
    permissions = RolePermissionRepository(db)
    ResourceRepository(db).upsert("heart.tick", "")
    role = roles.create("read", "Read", "")
    permissions.grant(role.id, "heart.tick", Verb.GET)

    permissions.revoke(role.id, "heart.tick", Verb.GET)

    assert permissions.list_for_role(role.id) == []


def test_role_permission_revoke_of_ungranted_verb_is_a_no_op(db: Database) -> None:
    roles = RoleRepository(db)
    permissions = RolePermissionRepository(db)
    role = roles.create("read", "Read", "")

    permissions.revoke(role.id, "heart.tick", Verb.GET)


def test_effective_verbs_unions_across_roles(db: Database) -> None:
    roles = RoleRepository(db)
    permissions = RolePermissionRepository(db)
    ResourceRepository(db).upsert("heart.tick", "")
    read_role = roles.create("read", "Read", "")
    write_role = roles.create("write", "Write", "")
    permissions.grant(read_role.id, "heart.tick", Verb.GET)
    permissions.grant(write_role.id, "heart.tick", Verb.POST)

    verbs = permissions.effective_verbs(
        frozenset({read_role.id, write_role.id}), "heart.tick"
    )

    assert verbs == {Verb.GET, Verb.POST}


def test_effective_verbs_with_no_role_ids_short_circuits_to_empty(
    db: Database,
) -> None:
    assert RolePermissionRepository(db).effective_verbs(frozenset(), "heart.tick") == (
        frozenset()
    )


# --- UserRepository ------------------------------------------------------------


def test_user_create_also_creates_a_local_external_identity(db: Database) -> None:
    users = UserRepository(db)
    identities = ExternalIdentityRepository(db)

    user = users.create("alice", display_name="Alice")

    assert users.get(user.id) == user
    assert users.get_by_username("alice") == user
    assert user.active is True
    identity = identities.get_by_provider_subject("local", "alice")
    assert identity is not None
    assert identity.user_id == user.id


def test_user_create_with_local_identity_false_skips_the_local_row(
    db: Database,
) -> None:
    users = UserRepository(db)
    identities = ExternalIdentityRepository(db)

    user = users.create("bootstrap-admin", local_identity=False)

    assert identities.list_for_user(user.id) == []
    assert identities.get_by_provider_subject("local", "bootstrap-admin") is None


def test_user_create_rejects_a_duplicate_username(db: Database) -> None:
    users = UserRepository(db)
    users.create("alice")

    with pytest.raises(DuplicateError, match="alice"):
        users.create("alice")


def test_user_get_of_unknown_id_returns_none(db: Database) -> None:
    users = UserRepository(db)
    assert users.get("no-such-id") is None
    assert users.get_by_username("no-such-user") is None


def test_user_set_active(db: Database) -> None:
    users = UserRepository(db)
    user = users.create("alice")

    users.set_active(user.id, active=False)

    updated = users.get(user.id)
    assert updated is not None
    assert updated.active is False


def test_user_list_all_orders_by_username(db: Database) -> None:
    users = UserRepository(db)
    users.create("bob")
    users.create("alice")

    assert [u.username for u in users.list_all()] == ["alice", "bob"]


# --- GroupRepository ------------------------------------------------------------


def test_group_create_and_get(db: Database) -> None:
    groups = GroupRepository(db)

    group = groups.create("ops", "Operations", description="ops team")

    assert groups.get(group.id) == group
    assert group.description == "ops team"


def test_group_get_of_unknown_id_returns_none(db: Database) -> None:
    assert GroupRepository(db).get("no-such-id") is None


def test_group_create_rejects_a_duplicate_slug(db: Database) -> None:
    groups = GroupRepository(db)
    groups.create("ops", "Operations")

    with pytest.raises(DuplicateError, match="ops"):
        groups.create("ops", "Operations Again")


def test_group_add_member_is_idempotent(db: Database) -> None:
    groups = GroupRepository(db)
    users = UserRepository(db)
    group = groups.create("ops", "Operations")
    user = users.create("alice")

    groups.add_member(group.id, user.id)
    groups.add_member(group.id, user.id)


def test_group_list_all_orders_by_slug(db: Database) -> None:
    groups = GroupRepository(db)
    groups.create("zeta", "Zeta")
    groups.create("alpha", "Alpha")

    assert [g.slug for g in groups.list_all()] == ["alpha", "zeta"]


# --- RoleAssignmentRepository ---------------------------------------------------


def test_role_assignment_assign_direct_to_user(db: Database) -> None:
    users = UserRepository(db)
    roles = RoleRepository(db)
    assignments = RoleAssignmentRepository(db)
    user = users.create("alice")
    role = roles.create("read", "Read", "")

    assignment = assignments.assign(SubjectType.USER, user.id, role.id)

    assert assignment.subject_type is SubjectType.USER
    assert [r.id for r in assignments.roles_for_user(user.id)] == [role.id]


def test_role_assignment_assign_is_idempotent(db: Database) -> None:
    users = UserRepository(db)
    roles = RoleRepository(db)
    assignments = RoleAssignmentRepository(db)
    user = users.create("alice")
    role = roles.create("read", "Read", "")

    first = assignments.assign(SubjectType.USER, user.id, role.id)
    second = assignments.assign(SubjectType.USER, user.id, role.id)

    assert first.id == second.id
    assert len(assignments.roles_for_user(user.id)) == 1


def test_role_assignment_assign_refuses_an_unknown_user(db: Database) -> None:
    roles = RoleRepository(db)
    assignments = RoleAssignmentRepository(db)
    role = roles.create("read", "Read", "")

    with pytest.raises(UnknownSubjectError, match="user"):
        assignments.assign(SubjectType.USER, "no-such-user", role.id)


def test_role_assignment_assign_refuses_an_unknown_group(db: Database) -> None:
    roles = RoleRepository(db)
    assignments = RoleAssignmentRepository(db)
    role = roles.create("read", "Read", "")

    with pytest.raises(UnknownSubjectError, match="group"):
        assignments.assign(SubjectType.GROUP, "no-such-group", role.id)


def test_role_assignment_via_group_membership_is_reachable(db: Database) -> None:
    users = UserRepository(db)
    groups = GroupRepository(db)
    roles = RoleRepository(db)
    assignments = RoleAssignmentRepository(db)
    user = users.create("alice")
    group = groups.create("ops", "Operations")
    groups.add_member(group.id, user.id)
    role = roles.create("write", "Write", "")
    assignments.assign(SubjectType.GROUP, group.id, role.id)

    assert [r.id for r in assignments.roles_for_user(user.id)] == [role.id]


def test_role_assignment_unassign(db: Database) -> None:
    users = UserRepository(db)
    roles = RoleRepository(db)
    assignments = RoleAssignmentRepository(db)
    user = users.create("alice")
    role = roles.create("read", "Read", "")
    assignments.assign(SubjectType.USER, user.id, role.id)

    assignments.unassign(SubjectType.USER, user.id, role.id)

    assert assignments.roles_for_user(user.id) == []


def test_role_assignment_unassign_of_unassigned_is_a_no_op(db: Database) -> None:
    users = UserRepository(db)
    roles = RoleRepository(db)
    assignments = RoleAssignmentRepository(db)
    user = users.create("alice")
    role = roles.create("read", "Read", "")

    assignments.unassign(SubjectType.USER, user.id, role.id)


def test_roles_for_user_deduplicates_direct_and_group_grants_of_the_same_role(
    db: Database,
) -> None:
    users = UserRepository(db)
    groups = GroupRepository(db)
    roles = RoleRepository(db)
    assignments = RoleAssignmentRepository(db)
    user = users.create("alice")
    group = groups.create("ops", "Operations")
    groups.add_member(group.id, user.id)
    role = roles.create("write", "Write", "")
    assignments.assign(SubjectType.USER, user.id, role.id)
    assignments.assign(SubjectType.GROUP, group.id, role.id)

    assert [r.id for r in assignments.roles_for_user(user.id)] == [role.id]


# --- CredentialRepository -------------------------------------------------------


def test_credential_set_password_and_verify(
    db: Database, cheap_argon2: Argon2Params
) -> None:
    users = UserRepository(db)
    credentials = CredentialRepository(db)
    user = users.create("alice")

    credential = credentials.set_password(user.id, "hunter2", cheap_argon2)

    assert credential.type is CredentialType.PASSWORD
    assert credential.salt is None
    assert "hunter2" not in credential.hash
    assert credentials.verify_password(user.id, "hunter2", cheap_argon2) is True
    assert credentials.verify_password(user.id, "wrong", cheap_argon2) is False


def test_credential_verify_password_with_no_credential_returns_false(
    db: Database, cheap_argon2: Argon2Params
) -> None:
    users = UserRepository(db)
    credentials = CredentialRepository(db)
    user = users.create("alice")

    assert credentials.verify_password(user.id, "hunter2", cheap_argon2) is False


def test_credential_set_password_replaces_the_previous_one(
    db: Database, cheap_argon2: Argon2Params
) -> None:
    users = UserRepository(db)
    credentials = CredentialRepository(db)
    user = users.create("alice")
    credentials.set_password(user.id, "first", cheap_argon2)

    credentials.set_password(user.id, "second", cheap_argon2)

    assert credentials.verify_password(user.id, "first", cheap_argon2) is False
    assert credentials.verify_password(user.id, "second", cheap_argon2) is True


def test_credential_create_and_find_api_token(db: Database) -> None:
    users = UserRepository(db)
    credentials = CredentialRepository(db)
    user = users.create("alice")

    credential = credentials.create_api_token(user.id, "a-real-token", label="cli")

    assert credential.type is CredentialType.API_TOKEN
    assert credential.salt is not None
    assert "a-real-token" not in credential.hash
    found = credentials.find_user_by_api_token("a-real-token")
    assert found is not None
    assert found.id == user.id


def test_credential_find_user_by_api_token_rejects_a_wrong_token(
    db: Database,
) -> None:
    users = UserRepository(db)
    credentials = CredentialRepository(db)
    user = users.create("alice")
    credentials.create_api_token(user.id, "a-real-token")

    assert credentials.find_user_by_api_token("a-wrong-token") is None


def test_credential_find_user_by_api_token_with_no_tokens_returns_none(
    db: Database,
) -> None:
    assert CredentialRepository(db).find_user_by_api_token("anything") is None


def test_credential_replace_api_token_revokes_the_old_one(db: Database) -> None:
    users = UserRepository(db)
    credentials = CredentialRepository(db)
    user = users.create("alice")
    credentials.create_api_token(user.id, "old-token")

    credentials.replace_api_token(user.id, "new-token")

    assert credentials.find_user_by_api_token("old-token") is None
    found = credentials.find_user_by_api_token("new-token")
    assert found is not None
    assert found.id == user.id


def test_credential_delete_credentials(db: Database) -> None:
    users = UserRepository(db)
    credentials = CredentialRepository(db)
    user = users.create("alice")
    credentials.create_api_token(user.id, "a-token")

    credentials.delete_credentials(user.id, CredentialType.API_TOKEN)

    assert credentials.find_user_by_api_token("a-token") is None


# --- ExternalIdentityRepository --------------------------------------------------


def test_external_identity_create_and_list_for_user(db: Database) -> None:
    users = UserRepository(db)
    identities = ExternalIdentityRepository(db)
    user = users.create("alice")  # already has one 'local' identity

    identities.create(user.id, "local-bootstrap", "alice")

    listed = identities.list_for_user(user.id)
    assert {i.provider for i in listed} == {"local", "local-bootstrap"}


def test_external_identity_get_by_provider_subject_of_unknown_returns_none(
    db: Database,
) -> None:
    assert (
        ExternalIdentityRepository(db).get_by_provider_subject("local", "nobody")
        is None
    )


def test_external_identity_create_rejects_a_duplicate_provider_subject(
    db: Database,
) -> None:
    users = UserRepository(db)
    identities = ExternalIdentityRepository(db)
    user = users.create("alice")

    with pytest.raises(DuplicateError, match="local"):
        identities.create(user.id, "local", "alice")
