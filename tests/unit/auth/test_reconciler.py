from __future__ import annotations

from ansina.auth.models import RoleSlug, Verb
from ansina.auth.policy import ResourceSpec, permitted_verbs
from ansina.auth.reconciler import reconcile_builtin_roles, sync_resources
from ansina.auth.repositories import (
    ResourceRepository,
    RolePermissionRepository,
    RoleRepository,
)
from ansina.storage.database import Database

_SPECS = (
    ResourceSpec("heart.tick", "tick loop"),
    ResourceSpec("auth.users", "user management"),
)


def test_sync_resources_creates_every_named_resource(db: Database) -> None:
    sync_resources(db, _SPECS)

    names = {r.name for r in ResourceRepository(db).list_all()}
    assert names == {"heart.tick", "auth.users"}


def test_sync_resources_updates_a_changed_description(db: Database) -> None:
    sync_resources(db, _SPECS)

    sync_resources(db, (ResourceSpec("heart.tick", "updated"), _SPECS[1]))

    resource = next(
        r for r in ResourceRepository(db).list_all() if r.name == "heart.tick"
    )
    assert resource.description == "updated"


def test_sync_resources_retires_a_resource_no_longer_declared(db: Database) -> None:
    sync_resources(db, _SPECS)

    sync_resources(db, (_SPECS[0],))

    names = {r.name for r in ResourceRepository(db).list_all()}
    assert names == {"heart.tick"}


def test_reconcile_builtin_roles_seeds_all_four_roles(db: Database) -> None:
    sync_resources(db, _SPECS)

    reconcile_builtin_roles(db)

    roles = RoleRepository(db)
    slugs = {r.slug for r in roles.list_all()}
    assert slugs == {slug.value for slug in RoleSlug}
    assert all(r.builtin for r in roles.list_all())


def test_reconcile_builtin_roles_produces_exactly_the_policy_predicted_grants(
    db: Database,
) -> None:
    sync_resources(db, _SPECS)

    reconcile_builtin_roles(db)

    roles = RoleRepository(db)
    permissions = RolePermissionRepository(db)
    for slug in RoleSlug:
        role = roles.get_by_slug(slug.value)
        assert role is not None
        actual = {(p.resource, p.verb) for p in permissions.list_for_role(role.id)}
        expected = {
            (spec.name, verb)
            for spec in _SPECS
            for verb in permitted_verbs(slug, spec.name)
        }
        assert actual == expected


def test_reconcile_builtin_roles_is_idempotent(db: Database) -> None:
    sync_resources(db, _SPECS)
    reconcile_builtin_roles(db)
    roles = RoleRepository(db)
    permissions = RolePermissionRepository(db)
    admin = roles.get_by_slug(RoleSlug.ADMIN.value)
    assert admin is not None
    before = {(p.resource, p.verb) for p in permissions.list_for_role(admin.id)}

    reconcile_builtin_roles(db)

    after = {(p.resource, p.verb) for p in permissions.list_for_role(admin.id)}
    assert before == after


def test_reconcile_builtin_roles_prunes_a_stale_grant(db: Database) -> None:
    sync_resources(db, _SPECS)
    reconcile_builtin_roles(db)
    roles = RoleRepository(db)
    permissions = RolePermissionRepository(db)
    read_role = roles.get_by_slug(RoleSlug.READ.value)
    assert read_role is not None
    # Hand-insert a grant the fixed policy never predicts for Read (POST) — the
    # resource itself stays catalogued throughout, isolating "reconciler prunes a
    # non-policy-predicted row" from "a resource left the catalog".
    permissions.grant(read_role.id, "heart.tick", Verb.POST)

    reconcile_builtin_roles(db)

    remaining = {(p.resource, p.verb) for p in permissions.list_for_role(read_role.id)}
    assert ("heart.tick", Verb.POST) not in remaining
    # The policy-predicted GET grant survives untouched.
    assert ("heart.tick", Verb.GET) in remaining


def test_reconcile_builtin_roles_never_touches_a_non_builtin_roles_grants(
    db: Database,
) -> None:
    sync_resources(db, _SPECS)
    roles = RoleRepository(db)
    permissions = RolePermissionRepository(db)
    custom = roles.create("custom", "Custom", "a hand-defined role")
    permissions.grant(custom.id, "heart.tick", Verb.GET)

    reconcile_builtin_roles(db)

    assert [(p.resource, p.verb) for p in permissions.list_for_role(custom.id)] == [
        ("heart.tick", Verb.GET)
    ]
    refreshed = roles.get(custom.id)
    assert refreshed is not None
    assert refreshed.builtin is False
