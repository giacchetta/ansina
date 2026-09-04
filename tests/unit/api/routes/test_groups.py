from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ansina.auth.models import SubjectType
from ansina.auth.repositories import (
    RoleAssignmentRepository,
    RoleRepository,
    UserRepository,
)

# --- role gating ----------------------------------------------------------------------


def test_read_role_gets_403_on_list(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("read")

    response = authed_client.get(
        "/auth/groups", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.forbidden"


def test_maintain_without_sudo_gets_get_with_no_grant(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("maintain")

    response = authed_client.get(
        "/auth/groups", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200


def test_maintain_without_sudo_gets_403_on_create(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("maintain")

    response = authed_client.post(
        "/auth/groups",
        headers={"Authorization": f"Bearer {token}"},
        json={"slug": "nope", "name": "Nope"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.auth.sudo_required"


def test_maintain_with_sudo_grant_can_create(
    authed_client: TestClient, sudoed_maintain: Callable[[], dict[str, str]]
) -> None:
    response = authed_client.post(
        "/auth/groups",
        headers=sudoed_maintain(),
        json={"slug": "engineers", "name": "Engineers"},
    )

    assert response.status_code == 201


# --- CRUD -----------------------------------------------------------------------------


def test_create_get_list_round_trip(
    authed_client: TestClient, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    created = authed_client.post(
        "/auth/groups",
        headers=headers,
        json={"slug": "ops", "name": "Ops", "description": "Operations"},
    )
    assert created.status_code == 201
    group_id = created.json()["id"]

    listed = authed_client.get("/auth/groups", headers=headers)
    assert any(g["slug"] == "ops" for g in listed.json())

    fetched = authed_client.get(f"/auth/groups/{group_id}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Ops"


def test_get_unknown_group_is_404(authed_client: TestClient, authed_token: str) -> None:
    response = authed_client.get(
        "/auth/groups/nope", headers={"Authorization": f"Bearer {authed_token}"}
    )

    assert response.status_code == 404
    assert response.json()["code"] == "ansina.auth.not_found"


def test_create_duplicate_slug_is_409(
    authed_client: TestClient, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    authed_client.post(
        "/auth/groups", headers=headers, json={"slug": "dup", "name": "Dup"}
    )

    response = authed_client.post(
        "/auth/groups", headers=headers, json={"slug": "dup", "name": "Dup Again"}
    )

    assert response.status_code == 409


def test_patch_updates_name_and_description(
    authed_client: TestClient, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    created = authed_client.post(
        "/auth/groups", headers=headers, json={"slug": "patchme", "name": "Old"}
    ).json()

    response = authed_client.patch(
        f"/auth/groups/{created['id']}",
        headers=headers,
        json={"name": "New", "description": "updated"},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "New"
    assert response.json()["description"] == "updated"


def test_patch_unknown_group_is_404(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.patch(
        "/auth/groups/nope",
        headers={"Authorization": f"Bearer {authed_token}"},
        json={"name": "x"},
    )

    assert response.status_code == 404


def test_delete_unknown_group_is_404(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.delete(
        "/auth/groups/nope", headers={"Authorization": f"Bearer {authed_token}"}
    )

    assert response.status_code == 404


def test_delete_a_non_admin_group_succeeds(
    authed_client: TestClient, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    created = authed_client.post(
        "/auth/groups", headers=headers, json={"slug": "deleteme", "name": "Bye"}
    ).json()

    response = authed_client.delete(f"/auth/groups/{created['id']}", headers=headers)

    assert response.status_code == 204


def test_deleting_an_admin_group_that_would_leave_zero_admins_is_409(
    authed_app: FastAPI, authed_client: TestClient, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    db = authed_app.state.db
    bootstrap = UserRepository(db).get_by_username("bootstrap-admin")
    assert bootstrap is not None

    group = authed_client.post(
        "/auth/groups", headers=headers, json={"slug": "admins", "name": "Admins"}
    ).json()
    admin_role = RoleRepository(db).get_by_slug("admin")
    assert admin_role is not None
    authed_client.post(
        f"/auth/groups/{group['id']}/roles/{admin_role.id}", headers=headers
    )
    # Add the sole admin to the group *before* dropping their direct grant — dropping
    # it first would leave them with no admin-granting path at all to perform the add.
    authed_client.put(
        f"/auth/groups/{group['id']}/members/{bootstrap.id}", headers=headers
    )
    RoleAssignmentRepository(db).unassign(SubjectType.USER, bootstrap.id, admin_role.id)

    response = authed_client.delete(f"/auth/groups/{group['id']}", headers=headers)

    assert response.status_code == 409
    assert response.json()["code"] == "ansina.auth.last_admin"


def test_deleting_an_admin_group_with_a_spare_admin_succeeds(
    authed_app: FastAPI, authed_client: TestClient, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    db = authed_app.state.db
    group = authed_client.post(
        "/auth/groups", headers=headers, json={"slug": "admins2", "name": "Admins2"}
    ).json()
    admin_role = RoleRepository(db).get_by_slug("admin")
    assert admin_role is not None
    authed_client.post(
        f"/auth/groups/{group['id']}/roles/{admin_role.id}", headers=headers
    )
    other = UserRepository(db).create("spare-member")
    authed_client.put(f"/auth/groups/{group['id']}/members/{other.id}", headers=headers)
    # The bootstrap Admin is still directly an Admin too, so this group isn't the
    # sole remaining path to admin.

    response = authed_client.delete(f"/auth/groups/{group['id']}", headers=headers)

    assert response.status_code == 204


# --- membership -----------------------------------------------------------------------


def test_add_and_remove_member(authed_client: TestClient, authed_token: str) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    group = authed_client.post(
        "/auth/groups", headers=headers, json={"slug": "members", "name": "Members"}
    ).json()
    user = authed_client.post(
        "/auth/users", headers=headers, json={"username": "lisa"}
    ).json()

    add = authed_client.put(
        f"/auth/groups/{group['id']}/members/{user['id']}", headers=headers
    )
    assert add.status_code == 204

    remove = authed_client.delete(
        f"/auth/groups/{group['id']}/members/{user['id']}", headers=headers
    )
    assert remove.status_code == 204


def test_add_member_to_unknown_group_is_404(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.put(
        "/auth/groups/nope/members/someone",
        headers={"Authorization": f"Bearer {authed_token}"},
    )

    assert response.status_code == 404


def test_remove_member_from_unknown_group_is_404(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.delete(
        "/auth/groups/nope/members/someone",
        headers={"Authorization": f"Bearer {authed_token}"},
    )

    assert response.status_code == 404


def test_removing_the_last_admin_via_group_membership_is_409(
    authed_app: FastAPI, authed_client: TestClient, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    db = authed_app.state.db
    bootstrap = UserRepository(db).get_by_username("bootstrap-admin")
    assert bootstrap is not None
    group = authed_client.post(
        "/auth/groups", headers=headers, json={"slug": "admins3", "name": "Admins3"}
    ).json()
    admin_role = RoleRepository(db).get_by_slug("admin")
    assert admin_role is not None
    authed_client.post(
        f"/auth/groups/{group['id']}/roles/{admin_role.id}", headers=headers
    )
    authed_client.put(
        f"/auth/groups/{group['id']}/members/{bootstrap.id}", headers=headers
    )
    RoleAssignmentRepository(db).unassign(SubjectType.USER, bootstrap.id, admin_role.id)

    response = authed_client.delete(
        f"/auth/groups/{group['id']}/members/{bootstrap.id}", headers=headers
    )

    assert response.status_code == 409
    assert response.json()["code"] == "ansina.auth.last_admin"


def test_removing_a_member_from_a_non_admin_group_never_guards(
    authed_client: TestClient, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    group = authed_client.post(
        "/auth/groups", headers=headers, json={"slug": "plain", "name": "Plain"}
    ).json()
    user = authed_client.post(
        "/auth/users", headers=headers, json={"username": "mallory"}
    ).json()
    authed_client.put(
        f"/auth/groups/{group['id']}/members/{user['id']}", headers=headers
    )

    response = authed_client.delete(
        f"/auth/groups/{group['id']}/members/{user['id']}", headers=headers
    )

    assert response.status_code == 204
