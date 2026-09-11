"""Unit tests for `GET /auth/me` (issue #30) and `/auth/me/tokens` (issue #28).

Reuses `tests/unit/api/conftest.py`'s `authed_client`/`token_for_role`/`sudoed_maintain`
fixtures — the same shape `test_sudo.py`/`test_permissions.py` already use.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ansina.auth.models import RoleSlug
from ansina.auth.repositories import CredentialRepository, UserRepository


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


# --- /auth/me/tokens: issue #28's self-service token surface -----------------------


@pytest.mark.parametrize("role", list(RoleSlug))
def test_every_builtin_role_reaches_every_token_route(
    authed_client: TestClient, token_for_role: Callable[[str], str], role: RoleSlug
) -> None:
    """The `me.*` carve-out again: `Read` mints/lists/revokes its own tokens with no
    403, unlike every `auth.*` route.
    """
    token = token_for_role(role.value)
    headers = {"Authorization": f"Bearer {token}"}

    minted = authed_client.post("/auth/me/tokens", headers=headers, json={})
    assert minted.status_code == 201

    listed = authed_client.get("/auth/me/tokens", headers=headers)
    assert listed.status_code == 200

    revoked = authed_client.delete(
        f"/auth/me/tokens/{minted.json()['id']}", headers=headers
    )
    assert revoked.status_code == 204


def test_maintain_never_needs_sudo_for_tokens(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("maintain")
    headers = {"Authorization": f"Bearer {token}"}

    response = authed_client.post("/auth/me/tokens", headers=headers, json={})

    assert response.status_code == 201


def test_mint_returns_the_raw_token_exactly_once(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("read")
    headers = {"Authorization": f"Bearer {token}"}

    response = authed_client.post(
        "/auth/me/tokens", headers=headers, json={"label": "laptop"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["label"] == "laptop"
    assert body["token"]
    assert "hash" not in body
    assert "salt" not in body


def test_list_never_carries_a_hash_or_salt(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("read")
    headers = {"Authorization": f"Bearer {token}"}
    authed_client.post("/auth/me/tokens", headers=headers, json={"label": "laptop"})

    response = authed_client.get("/auth/me/tokens", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert any(entry["label"] == "laptop" for entry in body)
    assert "hash" not in str(body)
    assert "salt" not in str(body)


def test_two_concurrent_tokens_both_authenticate_and_are_independently_revocable(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    original = token_for_role("read")
    headers = {"Authorization": f"Bearer {original}"}
    laptop = authed_client.post(
        "/auth/me/tokens", headers=headers, json={"label": "laptop"}
    ).json()
    ci = authed_client.post(
        "/auth/me/tokens", headers=headers, json={"label": "ci"}
    ).json()

    for token in (laptop["token"], ci["token"]):
        assert (
            authed_client.get(
                "/auth/me", headers={"Authorization": f"Bearer {token}"}
            ).status_code
            == 200
        )

    revoke = authed_client.delete(
        f"/auth/me/tokens/{laptop['id']}",
        headers={"Authorization": f"Bearer {laptop['token']}"},
    )
    assert revoke.status_code == 204

    assert (
        authed_client.get(
            "/auth/me", headers={"Authorization": f"Bearer {laptop['token']}"}
        ).status_code
        == 401
    )
    # The other token is untouched by the sibling's revocation.
    assert (
        authed_client.get(
            "/auth/me", headers={"Authorization": f"Bearer {ci['token']}"}
        ).status_code
        == 200
    )


def test_revoking_another_users_token_id_is_404(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    victim = token_for_role("read")
    victim_headers = {"Authorization": f"Bearer {victim}"}
    victims_token = authed_client.post(
        "/auth/me/tokens", headers=victim_headers, json={}
    ).json()

    attacker = token_for_role("read")
    response = authed_client.delete(
        f"/auth/me/tokens/{victims_token['id']}",
        headers={"Authorization": f"Bearer {attacker}"},
    )

    assert response.status_code == 404
    # The victim's own token is untouched.
    assert (
        authed_client.get(
            "/auth/me", headers={"Authorization": f"Bearer {victims_token['token']}"}
        ).status_code
        == 200
    )


def test_mint_and_revoke_refuse_the_bootstrap_identity_but_get_allows(
    authed_app: FastAPI, authed_client: TestClient
) -> None:
    bootstrap = UserRepository(authed_app.state.db).get_by_username("bootstrap-admin")
    assert bootstrap is not None
    bootstrap_token = CredentialRepository(authed_app.state.db).list_api_tokens(
        bootstrap.id
    )[0]
    # The bootstrap identity's one live token isn't recoverable from a hash — seed a
    # second, test-only one directly at the repository layer (bypassing the route's
    # own refusal, which is exactly what this test is about) purely so there's a
    # bearer credential to authenticate the request itself.
    raw = "bootstrap-test-only-second-credential-abc123"
    CredentialRepository(authed_app.state.db).create_api_token(bootstrap.id, raw)
    headers = {"Authorization": f"Bearer {raw}"}

    minted = authed_client.post("/auth/me/tokens", headers=headers, json={})
    assert minted.status_code == 403
    assert minted.json()["code"] == "ansina.auth.bootstrap_identity"

    listed = authed_client.get("/auth/me/tokens", headers=headers)
    assert listed.status_code == 200

    revoked = authed_client.delete(
        f"/auth/me/tokens/{bootstrap_token.id}", headers=headers
    )
    assert revoked.status_code == 403
    assert revoked.json()["code"] == "ansina.auth.bootstrap_identity"


def test_token_routes_401_with_no_token(authed_client: TestClient) -> None:
    assert authed_client.post("/auth/me/tokens", json={}).status_code == 401
    assert authed_client.get("/auth/me/tokens").status_code == 401
    assert authed_client.delete("/auth/me/tokens/anything").status_code == 401


def test_token_routes_have_no_identity_when_security_is_disabled(
    client: TestClient,
) -> None:
    for response in (
        client.post("/auth/me/tokens", json={}),
        client.get("/auth/me/tokens"),
        client.delete("/auth/me/tokens/anything"),
    ):
        assert response.status_code == 401
        assert response.json()["code"] == "ansina.unauthorized"
