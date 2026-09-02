from __future__ import annotations

import pytest

from ansina.auth.authorization import ForbiddenError, SudoRequiredError, authorize
from ansina.auth.models import RoleSlug, User, Verb
from ansina.auth.principal import Principal
from ansina.auth.repositories import (
    ResourceRepository,
    RolePermissionRepository,
    RoleRepository,
)
from ansina.storage.database import Database

_USER = User(
    id="user-1",
    username="alice",
    display_name="",
    active=True,
    created_at="2026-01-01T00:00:00Z",
)


def _seed_role_with_grant(
    db: Database, slug: RoleSlug, resource: str, verbs: tuple[Verb, ...]
) -> str:
    """A resource catalogued, and one role granted `verbs` on it — returns the role
    id.
    """
    ResourceRepository(db).upsert(resource, "")
    role = RoleRepository(db).ensure_builtin(slug.value, slug.value.title(), "")
    for verb in verbs:
        RolePermissionRepository(db).grant(role.id, resource, verb)
    return role.id


def test_authorize_returns_none_when_the_verb_is_granted(db: Database) -> None:
    role_id = _seed_role_with_grant(db, RoleSlug.READ, "heart.tick", (Verb.GET,))
    principal = Principal(user=_USER, role_ids=frozenset({role_id}))

    authorize(db, principal, "heart.tick", Verb.GET)  # must not raise


def test_authorize_raises_forbidden_when_no_role_grants_the_verb(db: Database) -> None:
    role_id = _seed_role_with_grant(db, RoleSlug.READ, "heart.tick", (Verb.GET,))
    principal = Principal(user=_USER, role_ids=frozenset({role_id}))

    with pytest.raises(ForbiddenError) as excinfo:
        authorize(db, principal, "heart.tick", Verb.POST)

    assert excinfo.value.code == "ansina.forbidden"
    assert excinfo.value.details == {"resource": "heart.tick", "verb": "POST"}


def test_authorize_raises_forbidden_for_an_empty_role_set(db: Database) -> None:
    ResourceRepository(db).upsert("heart.tick", "")
    principal = Principal(user=_USER, role_ids=frozenset())

    with pytest.raises(ForbiddenError):
        authorize(db, principal, "heart.tick", Verb.GET)


def test_sensitive_with_admin_role_never_requires_sudo(db: Database) -> None:
    role_id = _seed_role_with_grant(db, RoleSlug.ADMIN, "auth.users", (Verb.DELETE,))
    principal = Principal(
        user=_USER,
        role_ids=frozenset({role_id}),
        role_slugs=frozenset({RoleSlug.ADMIN.value}),
    )

    authorize(db, principal, "auth.users", Verb.DELETE, sensitive=True)  # no raise


def test_sensitive_with_maintain_role_and_no_sudo_grant_requires_sudo(
    db: Database,
) -> None:
    role_id = _seed_role_with_grant(db, RoleSlug.MAINTAIN, "auth.users", (Verb.DELETE,))
    principal = Principal(
        user=_USER,
        role_ids=frozenset({role_id}),
        role_slugs=frozenset({RoleSlug.MAINTAIN.value}),
        sudo_active=False,
    )

    with pytest.raises(SudoRequiredError) as excinfo:
        authorize(db, principal, "auth.users", Verb.DELETE, sensitive=True)

    assert excinfo.value.code == "ansina.auth.sudo_required"


def test_sensitive_with_maintain_role_and_a_live_sudo_grant_succeeds(
    db: Database,
) -> None:
    role_id = _seed_role_with_grant(db, RoleSlug.MAINTAIN, "auth.users", (Verb.DELETE,))
    principal = Principal(
        user=_USER,
        role_ids=frozenset({role_id}),
        role_slugs=frozenset({RoleSlug.MAINTAIN.value}),
        sudo_active=True,
    )

    authorize(db, principal, "auth.users", Verb.DELETE, sensitive=True)  # no raise


def test_forbidden_is_checked_before_sudo_required(db: Database) -> None:
    """A `maintain`-only caller with no grant at all on the resource gets `Forbidden`,
    not a sudo prompt for an action it couldn't take regardless.
    """
    ResourceRepository(db).upsert("auth.users", "")
    principal = Principal(
        user=_USER,
        role_ids=frozenset(),
        role_slugs=frozenset({RoleSlug.MAINTAIN.value}),
    )

    with pytest.raises(ForbiddenError):
        authorize(db, principal, "auth.users", Verb.DELETE, sensitive=True)


def test_sensitive_false_never_raises_sudo_required(db: Database) -> None:
    role_id = _seed_role_with_grant(db, RoleSlug.MAINTAIN, "auth.users", (Verb.DELETE,))
    principal = Principal(
        user=_USER,
        role_ids=frozenset({role_id}),
        role_slugs=frozenset({RoleSlug.MAINTAIN.value}),
        sudo_active=False,
    )

    authorize(db, principal, "auth.users", Verb.DELETE, sensitive=False)  # no raise
