from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ansina.auth.repositories import RoleMappingRepository, RoleRepository

# --- read: role gating ------------------------------------------------------------


def test_read_role_gets_403(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("read")

    response = authed_client.get(
        "/auth/role-mappings", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.forbidden"


def test_maintain_without_sudo_can_still_read(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("maintain")

    response = authed_client.get(
        "/auth/role-mappings", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.json() == []


# --- write: the sudo gate (issue #37's fail-closed-on-sensitivity change) ---------


def test_maintain_without_sudo_gets_403_on_create(
    authed_client: TestClient,
    authed_app: FastAPI,
    token_for_role: Callable[[str], str],
) -> None:
    role = RoleRepository(authed_app.state.db).get_by_slug("read")
    assert role is not None
    token = token_for_role("maintain")

    response = authed_client.post(
        "/auth/role-mappings",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "provider": "acme",
            "claim": "groups",
            "value": "ops",
            "role_id": role.id,
        },
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.auth.sudo_required"


def test_maintain_without_sudo_gets_403_on_delete(
    authed_client: TestClient,
    authed_app: FastAPI,
    authed_token: str,
    token_for_role: Callable[[str], str],
) -> None:
    role = RoleRepository(authed_app.state.db).get_by_slug("read")
    assert role is not None
    admin_headers = {"Authorization": f"Bearer {authed_token}"}
    created = authed_client.post(
        "/auth/role-mappings",
        headers=admin_headers,
        json={
            "provider": "acme",
            "claim": "groups",
            "value": "ops",
            "role_id": role.id,
        },
    ).json()
    maintain_headers = {"Authorization": f"Bearer {token_for_role('maintain')}"}

    response = authed_client.delete(
        f"/auth/role-mappings/{created['id']}", headers=maintain_headers
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.auth.sudo_required"


# --- Admin round trip --------------------------------------------------------------


def test_admin_create_list_and_delete_round_trip(
    authed_client: TestClient, authed_app: FastAPI, authed_token: str
) -> None:
    role = RoleRepository(authed_app.state.db).get_by_slug("write")
    assert role is not None
    headers = {"Authorization": f"Bearer {authed_token}"}

    created = authed_client.post(
        "/auth/role-mappings",
        headers=headers,
        json={
            "provider": "acme",
            "claim": "groups",
            "value": "ops-team",
            "role_id": role.id,
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["provider"] == "acme"
    assert body["claim"] == "groups"
    assert body["value"] == "ops-team"
    assert body["role_id"] == role.id

    listed = authed_client.get("/auth/role-mappings", headers=headers)
    assert listed.status_code == 200
    assert any(m["id"] == body["id"] for m in listed.json())

    deleted = authed_client.delete(f"/auth/role-mappings/{body['id']}", headers=headers)
    assert deleted.status_code == 204
    assert RoleMappingRepository(authed_app.state.db).get(body["id"]) is None


def test_sudoed_maintain_can_create_and_delete(
    authed_client: TestClient,
    authed_app: FastAPI,
    sudoed_maintain: Callable[[], dict[str, str]],
) -> None:
    role = RoleRepository(authed_app.state.db).get_by_slug("write")
    assert role is not None

    created = authed_client.post(
        "/auth/role-mappings",
        headers=sudoed_maintain(),
        json={
            "provider": "acme",
            "claim": "groups",
            "value": "ops",
            "role_id": role.id,
        },
    )
    assert created.status_code == 201

    deleted = authed_client.delete(
        f"/auth/role-mappings/{created.json()['id']}", headers=sudoed_maintain()
    )
    assert deleted.status_code == 204


# --- validation ---------------------------------------------------------------------


def test_create_with_unknown_role_is_404(
    authed_client: TestClient, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}

    response = authed_client.post(
        "/auth/role-mappings",
        headers=headers,
        json={
            "provider": "acme",
            "claim": "groups",
            "value": "ops",
            "role_id": "does-not-exist",
        },
    )

    assert response.status_code == 404
    assert response.json()["code"] == "ansina.auth.not_found"


def test_create_duplicate_tuple_is_409(
    authed_client: TestClient, authed_app: FastAPI, authed_token: str
) -> None:
    role = RoleRepository(authed_app.state.db).get_by_slug("write")
    assert role is not None
    headers = {"Authorization": f"Bearer {authed_token}"}
    payload = {
        "provider": "acme",
        "claim": "groups",
        "value": "ops",
        "role_id": role.id,
    }
    first = authed_client.post("/auth/role-mappings", headers=headers, json=payload)
    assert first.status_code == 201

    second = authed_client.post("/auth/role-mappings", headers=headers, json=payload)

    assert second.status_code == 409
    assert second.json()["code"] == "ansina.auth.duplicate"


def test_delete_unknown_mapping_is_404(
    authed_client: TestClient, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}

    response = authed_client.delete(
        "/auth/role-mappings/does-not-exist", headers=headers
    )

    assert response.status_code == 404
    assert response.json()["code"] == "ansina.auth.not_found"


# --- self-escalation (a mapping is a deferred role assignment) --------------------


def test_sudoed_maintain_cannot_map_onto_admin(
    authed_app: FastAPI,
    authed_client: TestClient,
    sudoed_maintain: Callable[[], dict[str, str]],
) -> None:
    admin_role = RoleRepository(authed_app.state.db).get_by_slug("admin")
    assert admin_role is not None

    response = authed_client.post(
        "/auth/role-mappings",
        headers=sudoed_maintain(),
        json={
            "provider": "acme",
            "claim": "groups",
            "value": "admins",
            "role_id": admin_role.id,
        },
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.auth.self_escalation"


def test_create_in_dev_mode_skips_the_self_escalation_guard(
    client: TestClient, app: FastAPI
) -> None:
    """`security.enabled = false` never resolves a `Principal` — there's no "caller's
    own grants" to check the mapping against, so the guard is skipped entirely,
    mirroring `routes/role_assignments.py`'s own dev-mode carve-out.
    """
    admin_role = RoleRepository(app.state.db).get_by_slug("admin")
    assert admin_role is not None

    response = client.post(
        "/auth/role-mappings",
        json={
            "provider": "acme",
            "claim": "groups",
            "value": "admins",
            "role_id": admin_role.id,
        },
    )

    assert response.status_code == 201
