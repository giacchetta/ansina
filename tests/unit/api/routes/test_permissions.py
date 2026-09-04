from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient


def test_read_role_gets_403(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("read")

    response = authed_client.get(
        "/auth/permissions", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.forbidden"


def test_maintain_without_sudo_can_still_read(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("maintain")

    response = authed_client.get(
        "/auth/permissions", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200


def test_lists_every_catalogued_resource_crossed_with_every_verb(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.get(
        "/auth/permissions", headers={"Authorization": f"Bearer {authed_token}"}
    )

    assert response.status_code == 200
    body = response.json()
    by_resource = {entry["resource"]: entry for entry in body}
    assert "auth.users" in by_resource
    assert set(by_resource["auth.users"]["verbs"]) == {
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
    }
    assert "system.version" in by_resource
