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


def test_read_role_gets_403(
    authed_client: TestClient,
    authed_app: FastAPI,
    token_for_role: Callable[[str], str],
) -> None:
    read_role = RoleRepository(authed_app.state.db).get_by_slug("read")
    assert read_role is not None
    token = token_for_role("read")

    response = authed_client.post(
        f"/auth/users/does-not-matter/roles/{read_role.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.forbidden"


def test_maintain_without_sudo_gets_403_sudo_required(
    authed_client: TestClient,
    authed_app: FastAPI,
    token_for_role: Callable[[str], str],
) -> None:
    read_role = RoleRepository(authed_app.state.db).get_by_slug("read")
    assert read_role is not None
    token = token_for_role("maintain")

    response = authed_client.post(
        f"/auth/users/does-not-matter/roles/{read_role.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.auth.sudo_required"


# --- assign to a user -----------------------------------------------------------------


def test_assign_role_to_user_succeeds(
    authed_client: TestClient, authed_app: FastAPI, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    user = authed_client.post(
        "/auth/users", headers=headers, json={"username": "nate"}
    ).json()
    read_role = RoleRepository(authed_app.state.db).get_by_slug("read")
    assert read_role is not None

    response = authed_client.post(
        f"/auth/users/{user['id']}/roles/{read_role.id}", headers=headers
    )

    assert response.status_code == 204
    roles = RoleAssignmentRepository(authed_app.state.db).roles_for_user(user["id"])
    assert any(r.slug == "read" for r in roles)


def test_assign_unknown_role_to_user_is_404(
    authed_client: TestClient, authed_app: FastAPI, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    user = authed_client.post(
        "/auth/users", headers=headers, json={"username": "olga"}
    ).json()

    response = authed_client.post(
        f"/auth/users/{user['id']}/roles/does-not-exist", headers=headers
    )

    assert response.status_code == 404
    assert response.json()["code"] == "ansina.auth.not_found"


def test_assign_role_to_unknown_user_is_404(
    authed_client: TestClient, authed_app: FastAPI, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    read_role = RoleRepository(authed_app.state.db).get_by_slug("read")
    assert read_role is not None

    response = authed_client.post(
        f"/auth/users/does-not-exist/roles/{read_role.id}", headers=headers
    )

    assert response.status_code == 404
    assert response.json()["code"] == "ansina.auth.unknown_subject"


def test_sudoed_maintain_cannot_assign_admin_to_a_user(
    authed_client: TestClient,
    authed_app: FastAPI,
    authed_token: str,
    sudoed_maintain: Callable[[], dict[str, str]],
) -> None:
    admin_headers = {"Authorization": f"Bearer {authed_token}"}
    target = authed_client.post(
        "/auth/users", headers=admin_headers, json={"username": "peter"}
    ).json()
    admin_role = RoleRepository(authed_app.state.db).get_by_slug("admin")
    assert admin_role is not None

    response = authed_client.post(
        f"/auth/users/{target['id']}/roles/{admin_role.id}", headers=sudoed_maintain()
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.auth.self_escalation"


def test_assign_in_dev_mode_skips_the_self_escalation_guard(
    client: TestClient, app: FastAPI
) -> None:
    """`security.enabled = false` never resolves a `Principal` — there's no "caller's
    own grants" to check the assignment against, so the guard is skipped entirely.
    """
    user = UserRepository(app.state.db).create("quinn")
    admin_role = RoleRepository(app.state.db).get_by_slug("admin")
    assert admin_role is not None

    response = client.post(f"/auth/users/{user.id}/roles/{admin_role.id}")

    assert response.status_code == 204


def test_unassign_role_from_user_succeeds(
    authed_client: TestClient, authed_app: FastAPI, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    user = authed_client.post(
        "/auth/users", headers=headers, json={"username": "rosa"}
    ).json()
    read_role = RoleRepository(authed_app.state.db).get_by_slug("read")
    assert read_role is not None
    authed_client.post(
        f"/auth/users/{user['id']}/roles/{read_role.id}", headers=headers
    )

    response = authed_client.delete(
        f"/auth/users/{user['id']}/roles/{read_role.id}", headers=headers
    )

    assert response.status_code == 204
    roles = RoleAssignmentRepository(authed_app.state.db).roles_for_user(user["id"])
    assert not any(r.slug == "read" for r in roles)


def test_unassign_unknown_role_from_user_is_404(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.delete(
        "/auth/users/someone/roles/does-not-exist",
        headers={"Authorization": f"Bearer {authed_token}"},
    )

    assert response.status_code == 404


def test_unassigning_admin_from_the_sole_admin_is_409(
    authed_app: FastAPI, authed_client: TestClient, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    bootstrap = UserRepository(authed_app.state.db).get_by_username("bootstrap-admin")
    assert bootstrap is not None
    admin_role = RoleRepository(authed_app.state.db).get_by_slug("admin")
    assert admin_role is not None

    response = authed_client.delete(
        f"/auth/users/{bootstrap.id}/roles/{admin_role.id}", headers=headers
    )

    assert response.status_code == 409
    assert response.json()["code"] == "ansina.auth.last_admin"


# --- assign/unassign for a group ------------------------------------------------------


def test_assign_role_to_group_succeeds(
    authed_client: TestClient, authed_app: FastAPI, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    group = authed_client.post(
        "/auth/groups", headers=headers, json={"slug": "readers", "name": "Readers"}
    ).json()
    read_role = RoleRepository(authed_app.state.db).get_by_slug("read")
    assert read_role is not None

    response = authed_client.post(
        f"/auth/groups/{group['id']}/roles/{read_role.id}", headers=headers
    )

    assert response.status_code == 204
    roles = RoleAssignmentRepository(authed_app.state.db).list_for_subject(
        SubjectType.GROUP, group["id"]
    )
    assert any(r.slug == "read" for r in roles)


def test_assign_unknown_role_to_group_is_404(
    authed_client: TestClient, authed_app: FastAPI, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    group = authed_client.post(
        "/auth/groups", headers=headers, json={"slug": "grp2", "name": "Grp2"}
    ).json()

    response = authed_client.post(
        f"/auth/groups/{group['id']}/roles/does-not-exist", headers=headers
    )

    assert response.status_code == 404


def test_assign_role_to_unknown_group_is_404(
    authed_client: TestClient, authed_app: FastAPI, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    read_role = RoleRepository(authed_app.state.db).get_by_slug("read")
    assert read_role is not None

    response = authed_client.post(
        f"/auth/groups/does-not-exist/roles/{read_role.id}", headers=headers
    )

    assert response.status_code == 404
    assert response.json()["code"] == "ansina.auth.unknown_subject"


def test_sudoed_maintain_cannot_assign_an_auth_permission_role_to_a_group(
    authed_client: TestClient,
    authed_app: FastAPI,
    authed_token: str,
    sudoed_maintain: Callable[[], dict[str, str]],
) -> None:
    admin_headers = {"Authorization": f"Bearer {authed_token}"}
    group = authed_client.post(
        "/auth/groups", headers=admin_headers, json={"slug": "grp3", "name": "Grp3"}
    ).json()
    admin_role = RoleRepository(authed_app.state.db).get_by_slug("admin")
    assert admin_role is not None

    response = authed_client.post(
        f"/auth/groups/{group['id']}/roles/{admin_role.id}", headers=sudoed_maintain()
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.auth.self_escalation"


def test_unassign_role_from_group_succeeds(
    authed_client: TestClient, authed_app: FastAPI, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    group = authed_client.post(
        "/auth/groups", headers=headers, json={"slug": "grp4", "name": "Grp4"}
    ).json()
    read_role = RoleRepository(authed_app.state.db).get_by_slug("read")
    assert read_role is not None
    authed_client.post(
        f"/auth/groups/{group['id']}/roles/{read_role.id}", headers=headers
    )

    response = authed_client.delete(
        f"/auth/groups/{group['id']}/roles/{read_role.id}", headers=headers
    )

    assert response.status_code == 204
    roles = RoleAssignmentRepository(authed_app.state.db).list_for_subject(
        SubjectType.GROUP, group["id"]
    )
    assert not any(r.slug == "read" for r in roles)


def test_unassign_unknown_role_from_group_is_404(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.delete(
        "/auth/groups/some-group/roles/does-not-exist",
        headers={"Authorization": f"Bearer {authed_token}"},
    )

    assert response.status_code == 404


def test_unassigning_admin_from_a_group_that_would_leave_zero_admins_is_409(
    authed_app: FastAPI, authed_client: TestClient, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    db = authed_app.state.db
    bootstrap = UserRepository(db).get_by_username("bootstrap-admin")
    assert bootstrap is not None
    group = authed_client.post(
        "/auth/groups", headers=headers, json={"slug": "grp5", "name": "Grp5"}
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
        f"/auth/groups/{group['id']}/roles/{admin_role.id}", headers=headers
    )

    assert response.status_code == 409
    assert response.json()["code"] == "ansina.auth.last_admin"


def test_unassigning_admin_from_a_group_with_a_spare_admin_succeeds(
    authed_app: FastAPI, authed_client: TestClient, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    db = authed_app.state.db
    group = authed_client.post(
        "/auth/groups", headers=headers, json={"slug": "grp6", "name": "Grp6"}
    ).json()
    admin_role = RoleRepository(db).get_by_slug("admin")
    assert admin_role is not None
    authed_client.post(
        f"/auth/groups/{group['id']}/roles/{admin_role.id}", headers=headers
    )
    other = UserRepository(db).create("spare-admin-2")
    authed_client.put(f"/auth/groups/{group['id']}/members/{other.id}", headers=headers)

    response = authed_client.delete(
        f"/auth/groups/{group['id']}/roles/{admin_role.id}", headers=headers
    )

    assert response.status_code == 204


def test_unassigning_a_non_admin_role_from_a_group_never_guards(
    authed_client: TestClient, authed_app: FastAPI, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    group = authed_client.post(
        "/auth/groups", headers=headers, json={"slug": "grp7", "name": "Grp7"}
    ).json()
    read_role = RoleRepository(authed_app.state.db).get_by_slug("read")
    assert read_role is not None
    authed_client.post(
        f"/auth/groups/{group['id']}/roles/{read_role.id}", headers=headers
    )

    response = authed_client.delete(
        f"/auth/groups/{group['id']}/roles/{read_role.id}", headers=headers
    )

    assert response.status_code == 204
