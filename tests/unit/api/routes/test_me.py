"""Unit tests for `GET /auth/me`. See issue #30.

Reuses `tests/unit/api/conftest.py`'s `authed_client`/`token_for_role`/`sudoed_maintain`
fixtures — the same shape `test_sudo.py`/`test_permissions.py` already use.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from ansina.auth.models import RoleSlug


@pytest.mark.parametrize("role", list(RoleSlug))
def test_every_builtin_role_gets_200_no_403(
    authed_client: TestClient, token_for_role: Callable[[str], str], role: RoleSlug
) -> None:
    """The headline AC: no role — including `Read` — is forbidden here, unlike every
    other `auth.*` route where `Read`/`Write` get nothing.
    """
    token = token_for_role(role.value)

    response = authed_client.get(
        "/auth/me", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200


def test_maintain_never_needs_sudo(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("maintain")

    response = authed_client.get(
        "/auth/me", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.json()["sudo_active"] is False


def test_body_reflects_the_caller_own_identity(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("read")

    response = authed_client.get(
        "/auth/me", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["username"].startswith("read-user-")
    assert body["roles"] == ["read"]
    assert body["auth_method"] == "api_token"
    assert body["sudo_active"] is False
    assert body.get("user_id")


def test_sudo_active_reflects_a_live_grant(
    authed_client: TestClient, sudoed_maintain: Callable[[], dict[str, str]]
) -> None:
    headers = sudoed_maintain()

    response = authed_client.get("/auth/me", headers=headers)

    assert response.status_code == 200
    assert response.json()["sudo_active"] is True


def test_no_token_is_401(authed_client: TestClient) -> None:
    """The carve-out relaxes authorization, never authentication."""
    response = authed_client.get("/auth/me")

    assert response.status_code == 401
    assert response.json()["code"] == "ansina.unauthorized"


def test_security_disabled_has_no_identity_to_report(client: TestClient) -> None:
    """`security.enabled = false` (the `client` fixture's dev mode) never resolves a
    `Principal` — matches `routes/sudo.py`'s own no-identity convention.
    """
    response = client.get("/auth/me")

    assert response.status_code == 401
    assert response.json()["code"] == "ansina.unauthorized"
