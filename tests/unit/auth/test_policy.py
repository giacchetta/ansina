from __future__ import annotations

import pytest

from ansina.auth.models import RoleSlug, Verb
from ansina.auth.policy import (
    BUILTIN_ROLES,
    is_self_resource,
    is_sensitive_resource,
    permitted_verbs,
)


def test_builtin_roles_cover_exactly_the_four_slugs() -> None:
    assert {spec.slug for spec in BUILTIN_ROLES} == set(RoleSlug)


@pytest.mark.parametrize(
    ("resource", "expected"),
    [
        ("auth.users", True),
        ("auth.roles", True),
        ("heart.tick", False),
        ("system.version", False),
        ("me.profile", False),
    ],
)
def test_is_sensitive_resource(resource: str, expected: bool) -> None:
    assert is_sensitive_resource(resource) is expected


@pytest.mark.parametrize(
    ("resource", "expected"),
    [
        ("me.profile", True),
        ("me.tokens", True),
        ("auth.users", False),
        ("heart.tick", False),
        ("system.version", False),
    ],
)
def test_is_self_resource(resource: str, expected: bool) -> None:
    assert is_self_resource(resource) is expected


def test_read_role_gets_get_only_on_a_non_sensitive_resource() -> None:
    assert permitted_verbs(RoleSlug.READ, "heart.tick") == {Verb.GET}


def test_write_role_gets_get_and_mutating_verbs_but_not_delete() -> None:
    assert permitted_verbs(RoleSlug.WRITE, "heart.tick") == {
        Verb.GET,
        Verb.POST,
        Verb.PUT,
        Verb.PATCH,
    }


@pytest.mark.parametrize("role", [RoleSlug.MAINTAIN, RoleSlug.ADMIN])
def test_maintain_and_admin_get_every_verb_on_a_non_sensitive_resource(
    role: RoleSlug,
) -> None:
    assert permitted_verbs(role, "heart.tick") == set(Verb)


@pytest.mark.parametrize("role", [RoleSlug.READ, RoleSlug.WRITE])
def test_read_and_write_get_nothing_on_a_sensitive_resource(role: RoleSlug) -> None:
    assert permitted_verbs(role, "auth.users") == frozenset()


@pytest.mark.parametrize("role", [RoleSlug.MAINTAIN, RoleSlug.ADMIN])
def test_maintain_and_admin_get_every_verb_on_a_sensitive_resource(
    role: RoleSlug,
) -> None:
    assert permitted_verbs(role, "auth.users") == set(Verb)


@pytest.mark.parametrize("role", list(RoleSlug))
def test_every_builtin_role_gets_every_verb_on_a_self_resource(role: RoleSlug) -> None:
    """Issue #30's headline AC: `me.*` is unconditional — even `Read`, which gets
    nothing on a sensitive `auth.*` resource, gets every verb here.
    """
    assert permitted_verbs(role, "me.profile") == set(Verb)
