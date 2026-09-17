"""Tests for `ansina.auth.role_sync.sync_mapped_roles`. See issue #42.

Exercised standalone against a synthetic claims dict, exactly as the issue's own AC
requires — no OIDC/JWT code exists yet, and none of these tests need any.
"""

from __future__ import annotations

from ansina.auth.models import SubjectType
from ansina.auth.repositories import (
    RoleAssignmentRepository,
    RoleMappingRepository,
    RoleRepository,
    UserRepository,
)
from ansina.auth.role_sync import sync_mapped_roles
from ansina.storage.database import Database


def test_sync_adds_a_role_matching_a_string_claim(db: Database) -> None:
    user = UserRepository(db).create("alice")
    role = RoleRepository(db).create("ops", "Ops", "")
    RoleMappingRepository(db).create("acme", "department", "ops", role.id)

    sync_mapped_roles(db, user.id, "acme", {"department": "ops"})

    assignments = RoleAssignmentRepository(db)
    assert [r.id for r in assignments.roles_for_user(user.id)] == [role.id]
    assignment = assignments.list_for_subject(SubjectType.USER, user.id)[0]
    assert assignment.id == role.id


def test_sync_adds_a_role_matching_a_list_claim(db: Database) -> None:
    user = UserRepository(db).create("alice")
    role = RoleRepository(db).create("ops", "Ops", "")
    RoleMappingRepository(db).create("acme", "groups", "ops-team", role.id)

    sync_mapped_roles(
        db, user.id, "acme", {"groups": ["engineering", "ops-team", "on-call"]}
    )

    assert [r.id for r in RoleAssignmentRepository(db).roles_for_user(user.id)] == [
        role.id
    ]


def test_sync_records_source_as_the_provider(db: Database) -> None:
    user = UserRepository(db).create("alice")
    role = RoleRepository(db).create("ops", "Ops", "")
    RoleMappingRepository(db).create("acme", "department", "ops", role.id)

    sync_mapped_roles(db, user.id, "acme", {"department": "ops"})

    assert RoleAssignmentRepository(db).role_ids_for_subject(
        SubjectType.USER, user.id, source="acme"
    ) == frozenset({role.id})


def test_sync_removes_a_role_whose_claim_value_disappeared(db: Database) -> None:
    user = UserRepository(db).create("alice")
    role = RoleRepository(db).create("ops", "Ops", "")
    RoleMappingRepository(db).create("acme", "department", "ops", role.id)
    sync_mapped_roles(db, user.id, "acme", {"department": "ops"})

    sync_mapped_roles(db, user.id, "acme", {"department": "sales"})

    assert RoleAssignmentRepository(db).roles_for_user(user.id) == []


def test_sync_leaves_a_locally_assigned_role_untouched(db: Database) -> None:
    """AC: "a role manually assigned locally survives it." Since a `'local'` grant
    on the same (user, role) means the provider's own `assign` is a no-op (the
    unique index on `(subject_type, subject_id, role_id)` plus `ON CONFLICT ... DO
    NOTHING`), the row keeps `source='local'` even while the mapping is live — and
    removing the claim doesn't touch it either, since `sync_mapped_roles` only ever
    reads/writes `source='acme'` rows.
    """
    user = UserRepository(db).create("alice")
    role = RoleRepository(db).create("ops", "Ops", "")
    RoleAssignmentRepository(db).assign(
        SubjectType.USER, user.id, role.id, source="local"
    )
    RoleMappingRepository(db).create("acme", "department", "ops", role.id)

    sync_mapped_roles(db, user.id, "acme", {"department": "ops"})
    sync_mapped_roles(db, user.id, "acme", {"department": "sales"})

    assignments = RoleAssignmentRepository(db)
    assert [r.id for r in assignments.roles_for_user(user.id)] == [role.id]
    assignment = assignments.list_for_subject(SubjectType.USER, user.id)
    assert len(assignment) == 1


def test_sync_never_reads_or_removes_another_providers_assignment(
    db: Database,
) -> None:
    user = UserRepository(db).create("alice")
    other_role = RoleRepository(db).create("legacy", "Legacy", "")
    RoleAssignmentRepository(db).assign(
        SubjectType.USER, user.id, other_role.id, source="other-provider"
    )

    sync_mapped_roles(db, user.id, "acme", {"department": "ops"})

    assert RoleAssignmentRepository(db).role_ids_for_subject(
        SubjectType.USER, user.id, source="other-provider"
    ) == frozenset({other_role.id})


def test_sync_ignores_a_non_string_claim_value(db: Database) -> None:
    user = UserRepository(db).create("alice")
    role = RoleRepository(db).create("ops", "Ops", "")
    RoleMappingRepository(db).create("acme", "department", "1", role.id)

    sync_mapped_roles(db, user.id, "acme", {"department": 1})

    assert RoleAssignmentRepository(db).roles_for_user(user.id) == []


def test_sync_ignores_an_unknown_claim_key(db: Database) -> None:
    user = UserRepository(db).create("alice")
    role = RoleRepository(db).create("ops", "Ops", "")
    RoleMappingRepository(db).create("acme", "department", "ops", role.id)

    sync_mapped_roles(db, user.id, "acme", {"other_claim": "ops"})

    assert RoleAssignmentRepository(db).roles_for_user(user.id) == []


def test_sync_is_idempotent(db: Database) -> None:
    user = UserRepository(db).create("alice")
    role = RoleRepository(db).create("ops", "Ops", "")
    RoleMappingRepository(db).create("acme", "department", "ops", role.id)

    sync_mapped_roles(db, user.id, "acme", {"department": "ops"})
    sync_mapped_roles(db, user.id, "acme", {"department": "ops"})

    assert [r.id for r in RoleAssignmentRepository(db).roles_for_user(user.id)] == [
        role.id
    ]


def test_sync_with_no_mappings_for_provider_is_a_no_op(db: Database) -> None:
    user = UserRepository(db).create("alice")

    sync_mapped_roles(db, user.id, "acme", {"department": "ops"})

    assert RoleAssignmentRepository(db).roles_for_user(user.id) == []


def test_sync_only_adds_roles_whose_mapping_actually_matches(db: Database) -> None:
    user = UserRepository(db).create("alice")
    roles = RoleRepository(db)
    matched_role = roles.create("ops", "Ops", "")
    unmatched_role = roles.create("sales", "Sales", "")
    mappings = RoleMappingRepository(db)
    mappings.create("acme", "department", "ops", matched_role.id)
    mappings.create("acme", "department", "sales", unmatched_role.id)

    sync_mapped_roles(db, user.id, "acme", {"department": "ops"})

    assert [r.id for r in RoleAssignmentRepository(db).roles_for_user(user.id)] == [
        matched_role.id
    ]
