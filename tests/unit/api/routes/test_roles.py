from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ansina.auth.models import SubjectType
from ansina.auth.repositories import (
    RoleAssignmentRepository,
    RolePermissionRepository,
    RoleRepository,
    UserRepository,
)

# --- read: role gating -------------------------------------------------------------


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


def test_roles_and_permissions_never_disagree_about_what_is_grantable(
    authed_client: TestClient, authed_token: str
) -> None:
    """Issue #47 AC: `GET /auth/roles` and `GET /auth/permissions` never disagree —
    every builtin role's grant is on a verb its resource is listed as actually
    serving.
    """
    headers = {"Authorization": f"Bearer {authed_token}"}
    roles_body = authed_client.get("/auth/roles", headers=headers).json()
    permissions_body = authed_client.get("/auth/permissions", headers=headers).json()
    served_verbs = {
        entry["resource"]: set(entry["verbs"]) for entry in permissions_body
    }

    for role in roles_body:
        if not role["builtin"]:
            continue
        for grant in role["permissions"]:
            assert grant["verb"] in served_verbs[grant["resource"]]


# --- write: the sudo gate (issue #37's fail-closed-on-sensitivity change) ----------


def test_maintain_without_sudo_gets_403_on_create(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("maintain")

    response = authed_client.post(
        "/auth/roles",
        headers={"Authorization": f"Bearer {token}"},
        json={"slug": "ops", "name": "Ops"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.auth.sudo_required"


def test_maintain_without_sudo_gets_403_on_patch_and_delete(
    authed_client: TestClient,
    authed_token: str,
    token_for_role: Callable[[str], str],
) -> None:
    admin_headers = {"Authorization": f"Bearer {authed_token}"}
    created = authed_client.post(
        "/auth/roles", headers=admin_headers, json={"slug": "ops", "name": "Ops"}
    ).json()
    maintain_headers = {
        "Authorization": f"Bearer {token_for_role('maintain')}",
    }

    patched = authed_client.patch(
        f"/auth/roles/{created['id']}",
        headers=maintain_headers,
        json={"permissions": []},
    )
    deleted = authed_client.delete(
        f"/auth/roles/{created['id']}", headers=maintain_headers
    )

    assert patched.status_code == 403
    assert patched.json()["code"] == "ansina.auth.sudo_required"
    assert deleted.status_code == 403
    assert deleted.json()["code"] == "ansina.auth.sudo_required"


def test_maintain_with_sudo_grant_can_create_patch_and_delete(
    authed_client: TestClient,
    authed_token: str,
    sudoed_maintain: Callable[[], dict[str, str]],
) -> None:
    admin_headers = {"Authorization": f"Bearer {authed_token}"}
    created = authed_client.post(
        "/auth/roles",
        headers=admin_headers,
        json={"slug": "sudo-edit", "name": "Sudo Edit"},
    )
    assert created.status_code == 201
    role_id = created.json()["id"]
    grant_headers = sudoed_maintain()

    patched = authed_client.patch(
        f"/auth/roles/{role_id}",
        headers=grant_headers,
        json={"permissions": [{"resource": "heart.tick", "verb": "GET"}]},
    )
    deleted = authed_client.delete(f"/auth/roles/{role_id}", headers=grant_headers)

    assert patched.status_code == 200
    assert deleted.status_code == 204


# --- CRUD round trip -----------------------------------------------------------------


def test_create_patch_delete_round_trip(
    authed_client: TestClient, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}

    created = authed_client.post(
        "/auth/roles",
        headers=headers,
        json={
            "slug": "heart-watcher",
            "name": "Heart Watcher",
            "description": "Read-only Heart status.",
            "permissions": [{"resource": "heart.tick", "verb": "GET"}],
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["slug"] == "heart-watcher"
    assert body["builtin"] is False
    assert body["permissions"] == [{"resource": "heart.tick", "verb": "GET"}]
    role_id = body["id"]

    patched = authed_client.patch(
        f"/auth/roles/{role_id}",
        headers=headers,
        json={
            "permissions": [
                {"resource": "heart.tick", "verb": "GET"},
                {"resource": "heart.tick", "verb": "POST"},
            ]
        },
    )
    assert patched.status_code == 200
    assert {(g["resource"], g["verb"]) for g in patched.json()["permissions"]} == {
        ("heart.tick", "GET"),
        ("heart.tick", "POST"),
    }

    deleted = authed_client.delete(f"/auth/roles/{role_id}", headers=headers)
    assert deleted.status_code == 204

    listed = authed_client.get("/auth/roles", headers=headers)
    assert all(r["id"] != role_id for r in listed.json())


def test_create_duplicate_slug_is_409(
    authed_client: TestClient, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    authed_client.post(
        "/auth/roles", headers=headers, json={"slug": "dup-role", "name": "Dup"}
    )

    response = authed_client.post(
        "/auth/roles", headers=headers, json={"slug": "dup-role", "name": "Dup 2"}
    )

    assert response.status_code == 409


def test_patch_unknown_role_is_404(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.patch(
        "/auth/roles/nope",
        headers={"Authorization": f"Bearer {authed_token}"},
        json={"permissions": []},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "ansina.auth.not_found"


def test_delete_unknown_role_is_404(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.delete(
        "/auth/roles/nope", headers={"Authorization": f"Bearer {authed_token}"}
    )

    assert response.status_code == 404
    assert response.json()["code"] == "ansina.auth.not_found"


# --- builtin roles stay read-only, regardless of caller (issue #40) ----------------


def test_patch_a_builtin_role_is_409(
    authed_client: TestClient, authed_app: FastAPI, authed_token: str
) -> None:
    admin_role = RoleRepository(authed_app.state.db).get_by_slug("admin")
    assert admin_role is not None

    response = authed_client.patch(
        f"/auth/roles/{admin_role.id}",
        headers={"Authorization": f"Bearer {authed_token}"},
        json={"permissions": []},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "ansina.auth.builtin_role_immutable"


def test_delete_a_builtin_role_is_409(
    authed_client: TestClient, authed_app: FastAPI, authed_token: str
) -> None:
    admin_role = RoleRepository(authed_app.state.db).get_by_slug("admin")
    assert admin_role is not None

    response = authed_client.delete(
        f"/auth/roles/{admin_role.id}",
        headers={"Authorization": f"Bearer {authed_token}"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "ansina.auth.builtin_role_immutable"


# --- deleting an in-use role is refused by the repository layer, not by
# --- ON DELETE CASCADE (issue #40's resolution of M3's open consideration #9) ------


def test_delete_an_assigned_role_is_409(
    authed_client: TestClient, authed_app: FastAPI, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    created = authed_client.post(
        "/auth/roles", headers=headers, json={"slug": "assigned", "name": "Assigned"}
    ).json()
    db = authed_app.state.db
    user = UserRepository(db).create("role-holder")
    RoleAssignmentRepository(db).assign(SubjectType.USER, user.id, created["id"])

    response = authed_client.delete(f"/auth/roles/{created['id']}", headers=headers)

    assert response.status_code == 409
    assert response.json()["code"] == "ansina.auth.role_in_use"


def test_delete_an_unassigned_role_succeeds_and_drops_its_grants(
    authed_client: TestClient, authed_app: FastAPI, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    created = authed_client.post(
        "/auth/roles",
        headers=headers,
        json={
            "slug": "throwaway",
            "name": "Throwaway",
            "permissions": [{"resource": "heart.tick", "verb": "GET"}],
        },
    ).json()

    response = authed_client.delete(f"/auth/roles/{created['id']}", headers=headers)

    assert response.status_code == 204
    remaining = RolePermissionRepository(authed_app.state.db).list_for_role(
        created["id"]
    )
    assert remaining == []


# --- self-escalation checked at grant-edit time, not only at assignment
# --- (issue #40's resolution of M3's open consideration #8) -----------------------


def test_sudoed_maintain_cannot_grant_an_auth_resource_on_create(
    authed_client: TestClient, sudoed_maintain: Callable[[], dict[str, str]]
) -> None:
    response = authed_client.post(
        "/auth/roles",
        headers=sudoed_maintain(),
        json={
            "slug": "wannabe-admin",
            "name": "Wannabe Admin",
            "permissions": [{"resource": "auth.roles", "verb": "GET"}],
        },
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.auth.self_escalation"


def test_sudoed_maintain_cannot_grant_an_auth_resource_on_patch(
    authed_client: TestClient,
    authed_token: str,
    sudoed_maintain: Callable[[], dict[str, str]],
) -> None:
    admin_headers = {"Authorization": f"Bearer {authed_token}"}
    created = authed_client.post(
        "/auth/roles",
        headers=admin_headers,
        json={"slug": "innocuous", "name": "Innocuous"},
    ).json()

    response = authed_client.patch(
        f"/auth/roles/{created['id']}",
        headers=sudoed_maintain(),
        json={"permissions": [{"resource": "auth.roles", "verb": "GET"}]},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.auth.self_escalation"


# --- a submitted grant must be catalogued, grantable, and served (issue #38) ------


def test_create_refuses_an_uncatalogued_resource(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.post(
        "/auth/roles",
        headers={"Authorization": f"Bearer {authed_token}"},
        json={
            "slug": "bad-resource",
            "name": "Bad",
            "permissions": [{"resource": "no.such.resource", "verb": "GET"}],
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "ansina.auth.invalid_grant"


def test_create_refuses_a_non_grantable_self_resource(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.post(
        "/auth/roles",
        headers={"Authorization": f"Bearer {authed_token}"},
        json={
            "slug": "bad-self",
            "name": "Bad",
            "permissions": [{"resource": "me.profile", "verb": "GET"}],
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "ansina.auth.invalid_grant"


def test_create_refuses_a_verb_the_resource_does_not_serve(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.post(
        "/auth/roles",
        headers={"Authorization": f"Bearer {authed_token}"},
        json={
            "slug": "bad-verb",
            "name": "Bad",
            "permissions": [{"resource": "system.version", "verb": "POST"}],
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "ansina.auth.invalid_grant"
