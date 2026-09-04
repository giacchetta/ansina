from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ansina.auth.hashing import Argon2Params
from ansina.auth.repositories import (
    CredentialRepository,
    RoleRepository,
    UserRepository,
)

# --- role gating: auth.users restricts to Maintain/Admin, sensitive on mutation ------


def test_read_role_gets_403_on_get(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("read")

    response = authed_client.get(
        "/auth/users", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.forbidden"


def test_write_role_gets_403_on_create(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("write")

    response = authed_client.post(
        "/auth/users",
        headers={"Authorization": f"Bearer {token}"},
        json={"username": "nope"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.forbidden"


def test_maintain_without_sudo_gets_get_with_no_grant(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("maintain")

    response = authed_client.get(
        "/auth/users", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200


def test_maintain_without_sudo_gets_403_sudo_required_on_create(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("maintain")

    response = authed_client.post(
        "/auth/users",
        headers={"Authorization": f"Bearer {token}"},
        json={"username": "nope"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.auth.sudo_required"


def test_maintain_with_sudo_grant_can_create(
    authed_client: TestClient, sudoed_maintain: Callable[[], dict[str, str]]
) -> None:
    headers = sudoed_maintain()

    response = authed_client.post(
        "/auth/users", headers=headers, json={"username": "carol"}
    )

    assert response.status_code == 201


def test_admin_needs_no_grant_to_create(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.post(
        "/auth/users",
        headers={"Authorization": f"Bearer {authed_token}"},
        json={"username": "dave"},
    )

    assert response.status_code == 201


# --- POST /auth/users -----------------------------------------------------------------


def test_create_user_with_password_can_authenticate_with_it(
    authed_app: FastAPI, authed_client: TestClient, authed_token: str
) -> None:
    admin_headers = {"Authorization": f"Bearer {authed_token}"}

    create = authed_client.post(
        "/auth/users",
        headers=admin_headers,
        json={"username": "erin", "password": "correct horse battery staple"},
    )
    assert create.status_code == 201
    user_id = create.json()["id"]

    params = Argon2Params.from_settings(authed_app.state.settings)
    assert CredentialRepository(authed_app.state.db).verify_password(
        user_id, "correct horse battery staple", params
    )


def test_create_user_duplicate_username_is_409(
    authed_client: TestClient, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    authed_client.post("/auth/users", headers=headers, json={"username": "frank"})

    response = authed_client.post(
        "/auth/users", headers=headers, json={"username": "frank"}
    )

    assert response.status_code == 409
    assert response.json()["code"] == "ansina.auth.duplicate"


# --- GET /auth/users, GET /auth/users/{id} --------------------------------------------


def test_get_unknown_user_is_404(authed_client: TestClient, authed_token: str) -> None:
    response = authed_client.get(
        "/auth/users/does-not-exist",
        headers={"Authorization": f"Bearer {authed_token}"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "ansina.auth.not_found"


def test_list_and_get_round_trip(authed_client: TestClient, authed_token: str) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    created = authed_client.post(
        "/auth/users", headers=headers, json={"username": "grace"}
    ).json()

    listed = authed_client.get("/auth/users", headers=headers)
    assert listed.status_code == 200
    assert any(u["username"] == "grace" for u in listed.json())

    fetched = authed_client.get(f"/auth/users/{created['id']}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["username"] == "grace"


# --- PATCH /auth/users/{id} -----------------------------------------------------------


def test_patch_updates_display_name_and_active(
    authed_client: TestClient, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    created = authed_client.post(
        "/auth/users", headers=headers, json={"username": "heidi"}
    ).json()

    response = authed_client.patch(
        f"/auth/users/{created['id']}",
        headers=headers,
        json={"display_name": "Heidi H", "active": False},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "Heidi H"
    assert body["active"] is False


def test_patch_unknown_user_is_404(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.patch(
        "/auth/users/does-not-exist",
        headers={"Authorization": f"Bearer {authed_token}"},
        json={"display_name": "x"},
    )

    assert response.status_code == 404


def test_patch_deactivating_the_sole_admin_is_409(
    authed_app: FastAPI, authed_client: TestClient, authed_token: str
) -> None:
    bootstrap = UserRepository(authed_app.state.db).get_by_username("bootstrap-admin")
    assert bootstrap is not None

    response = authed_client.patch(
        f"/auth/users/{bootstrap.id}",
        headers={"Authorization": f"Bearer {authed_token}"},
        json={"active": False},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "ansina.auth.last_admin"


# --- DELETE /auth/users/{id} — one-way tombstone --------------------------------------


def test_delete_unknown_user_is_404(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.delete(
        "/auth/users/does-not-exist",
        headers={"Authorization": f"Bearer {authed_token}"},
    )

    assert response.status_code == 404


def test_delete_the_sole_admin_is_409(
    authed_app: FastAPI, authed_client: TestClient, authed_token: str
) -> None:
    bootstrap = UserRepository(authed_app.state.db).get_by_username("bootstrap-admin")
    assert bootstrap is not None

    response = authed_client.delete(
        f"/auth/users/{bootstrap.id}",
        headers={"Authorization": f"Bearer {authed_token}"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "ansina.auth.last_admin"


def test_delete_purges_the_users_token_and_is_idempotently_gone(
    authed_app: FastAPI, authed_client: TestClient, authed_token: str
) -> None:
    admin_headers = {"Authorization": f"Bearer {authed_token}"}
    created = authed_client.post(
        "/auth/users", headers=admin_headers, json={"username": "ivan"}
    ).json()
    token_resp = authed_client.post(
        f"/auth/users/{created['id']}/tokens", headers=admin_headers, json={}
    )
    ivan_token = token_resp.json()["token"]
    ivan_headers = {"Authorization": f"Bearer {ivan_token}"}
    # Token is live before deletion.
    assert authed_client.get("/auth/users", headers=ivan_headers).status_code == 403

    delete_response = authed_client.delete(
        f"/auth/users/{created['id']}", headers=admin_headers
    )
    assert delete_response.status_code == 204

    # The token no longer authenticates at all — not just no longer authorized.
    after_delete = authed_client.get("/auth/users", headers=ivan_headers)
    assert after_delete.status_code == 401

    # GET still shows the tombstoned user (audit visibility), deleted_at set.
    fetched = authed_client.get(f"/auth/users/{created['id']}", headers=admin_headers)
    assert fetched.status_code == 200
    assert fetched.json()["deleted_at"] is not None
    assert fetched.json()["active"] is False

    # Mutating routes now treat it as gone.
    assert (
        authed_client.patch(
            f"/auth/users/{created['id']}", headers=admin_headers, json={}
        ).status_code
        == 404
    )
    assert (
        authed_client.delete(
            f"/auth/users/{created['id']}", headers=admin_headers
        ).status_code
        == 404
    )
    assert (
        authed_client.put(
            f"/auth/users/{created['id']}/password",
            headers=admin_headers,
            json={"password": "whatever123"},
        ).status_code
        == 404
    )
    assert (
        authed_client.post(
            f"/auth/users/{created['id']}/tokens", headers=admin_headers, json={}
        ).status_code
        == 404
    )


# --- PUT /auth/users/{id}/password ----------------------------------------------------


def test_set_password_then_step_up_with_it(
    authed_app: FastAPI, authed_client: TestClient, authed_token: str
) -> None:
    admin_headers = {"Authorization": f"Bearer {authed_token}"}
    created = authed_client.post(
        "/auth/users", headers=admin_headers, json={"username": "judy"}
    ).json()
    role = RoleRepository(authed_app.state.db).get_by_slug("maintain")
    assert role is not None
    authed_client.post(
        f"/auth/users/{created['id']}/roles/{role.id}", headers=admin_headers
    )
    token_resp = authed_client.post(
        f"/auth/users/{created['id']}/tokens", headers=admin_headers, json={}
    )
    judy_token = token_resp.json()["token"]

    set_password = authed_client.put(
        f"/auth/users/{created['id']}/password",
        headers=admin_headers,
        json={"password": "a brand new password"},
    )
    assert set_password.status_code == 204

    step_up = authed_client.post(
        "/auth/sudo",
        headers={"Authorization": f"Bearer {judy_token}"},
        json={"password": "a brand new password"},
    )
    assert step_up.status_code == 200


def test_set_password_for_unknown_user_is_404(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.put(
        "/auth/users/does-not-exist/password",
        headers={"Authorization": f"Bearer {authed_token}"},
        json={"password": "whatever123"},
    )

    assert response.status_code == 404


# --- POST /auth/users/{id}/tokens -----------------------------------------------------


def test_issued_token_authenticates(
    authed_client: TestClient, authed_token: str
) -> None:
    admin_headers = {"Authorization": f"Bearer {authed_token}"}
    created = authed_client.post(
        "/auth/users", headers=admin_headers, json={"username": "kevin"}
    ).json()

    issued = authed_client.post(
        f"/auth/users/{created['id']}/tokens",
        headers=admin_headers,
        json={"label": "kevin's laptop"},
    )
    assert issued.status_code == 201
    assert issued.json()["label"] == "kevin's laptop"
    kevin_token = issued.json()["token"]

    # The token authenticates (401 -> would-be-401 path not hit); Kevin has no role
    # grants yet, so the authorized call itself is 403, not 401.
    response = authed_client.get(
        "/auth/users", headers={"Authorization": f"Bearer {kevin_token}"}
    )
    assert response.status_code == 403


def test_issue_token_for_unknown_user_is_404(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.post(
        "/auth/users/does-not-exist/tokens",
        headers={"Authorization": f"Bearer {authed_token}"},
        json={},
    )

    assert response.status_code == 404
