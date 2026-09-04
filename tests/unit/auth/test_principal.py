from __future__ import annotations

from ansina.auth.models import User
from ansina.auth.principal import AuthMethod, Principal

_USER = User(
    id="user-1",
    username="alice",
    display_name="Alice",
    active=True,
    created_at="2026-01-01T00:00:00Z",
)


def test_actor_is_the_users_username() -> None:
    principal = Principal(user=_USER, role_ids=frozenset({"role-1"}))

    assert principal.actor == "alice"


def test_defaults_are_api_token_and_no_live_sudo() -> None:
    principal = Principal(user=_USER, role_ids=frozenset())

    assert principal.auth_method is AuthMethod.API_TOKEN
    assert principal.sudo_active is False
    assert principal.sudo_grant_id is None
    assert principal.role_slugs == frozenset()


def test_with_sudo_elevates_without_mutating_the_original() -> None:
    principal = Principal(user=_USER, role_ids=frozenset({"role-1"}))

    elevated = principal.with_sudo("grant-1")

    assert elevated.sudo_active is True
    assert elevated.sudo_grant_id == "grant-1"
    assert principal.sudo_active is False
    assert principal.sudo_grant_id is None
