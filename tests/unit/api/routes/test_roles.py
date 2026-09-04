from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient


def test_read_role_gets_403(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("read")

    response = authed_client.get(
        "/auth/roles", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.forbidden"


def test_maintain_without_sudo_can_still_read(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("maintain")

    response = authed_client.get(
        "/auth/roles", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200


def test_lists_builtin_roles_with_their_grants(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.get(
        "/auth/roles", headers={"Authorization": f"Bearer {authed_token}"}
    )

    assert response.status_code == 200
    body = response.json()
    slugs = {role["slug"] for role in body}
    assert slugs == {"read", "write", "maintain", "admin"}
    for role in body:
        assert role["builtin"] is True
        assert isinstance(role["permissions"], list)
        assert all(
            {"resource", "verb"} <= grant.keys() for grant in role["permissions"]
        )

    admin = next(r for r in body if r["slug"] == "admin")
    assert any(g["resource"] == "auth.roles" for g in admin["permissions"])


def test_no_write_routes_exist_for_roles(
    authed_client: TestClient, authed_token: str
) -> None:
    """Read-only by omission (issue #27): `POST /auth/roles` hits the registered
    `/auth/roles` path with an unsupported method (405); no `/auth/roles/{id}` path
    is registered at all, so a `PATCH`/`DELETE` there is a plain 404 — there is no
    route to 405 on.
    """
    headers = {"Authorization": f"Bearer {authed_token}"}

    assert (
        authed_client.post("/auth/roles", headers=headers, json={}).status_code == 405
    )
    assert (
        authed_client.patch("/auth/roles/some-id", headers=headers, json={}).status_code
        == 404
    )
    assert (
        authed_client.delete("/auth/roles/some-id", headers=headers).status_code == 404
    )
