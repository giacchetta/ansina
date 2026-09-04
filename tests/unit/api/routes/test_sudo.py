from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ansina.auth.hashing import Argon2Params
from ansina.auth.models import SubjectType
from ansina.auth.repositories import (
    CredentialRepository,
    RoleAssignmentRepository,
    RoleRepository,
    SudoGrantRepository,
    UserRepository,
)

_CHEAP_ARGON2 = Argon2Params(time_cost=1, memory_cost_kib=8, parallelism=1)
_PASSWORD = "correct horse battery staple"


def _mint_maintain_with_password(authed_app: FastAPI, username: str) -> str:
    """A fresh `Maintain` user with both an api_token (`<username>-token`) and a
    password credential (`_PASSWORD`) — everything `POST /auth/sudo` needs.
    """
    db = authed_app.state.db
    user = UserRepository(db).create(username)
    role = RoleRepository(db).get_by_slug("maintain")
    assert role is not None
    RoleAssignmentRepository(db).assign(SubjectType.USER, user.id, role.id)
    token = f"{username}-token"
    CredentialRepository(db).create_api_token(user.id, token)
    CredentialRepository(db).set_password(user.id, _PASSWORD, _CHEAP_ARGON2)
    return token


# --- role gating: auth.* resources restrict to Maintain/Admin -----------------------


def test_read_role_gets_403_forbidden_on_step_up(
    authed_app: FastAPI,
    authed_client: TestClient,
    token_for_role: Callable[[str], str],
) -> None:
    token = token_for_role("read")

    response = authed_client.post(
        "/auth/sudo",
        headers={"Authorization": f"Bearer {token}"},
        json={"password": "irrelevant"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.forbidden"


# --- POST /auth/sudo -----------------------------------------------------------------


def test_correct_password_issues_a_grant(
    authed_app: FastAPI, authed_client: TestClient
) -> None:
    token = _mint_maintain_with_password(authed_app, "maintainer")

    response = authed_client.post(
        "/auth/sudo",
        headers={"Authorization": f"Bearer {token}"},
        json={"password": _PASSWORD},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["verifier"] == "password"
    assert isinstance(body["token"], str) and body["token"]
    assert "expires_at" in body


def test_wrong_password_is_401(authed_app: FastAPI, authed_client: TestClient) -> None:
    token = _mint_maintain_with_password(authed_app, "maintainer")

    response = authed_client.post(
        "/auth/sudo",
        headers={"Authorization": f"Bearer {token}"},
        json={"password": "not the password"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "ansina.unauthorized"


def test_locked_out_after_max_failed_attempts_is_429_with_retry_after(
    authed_app: FastAPI, authed_client: TestClient
) -> None:
    token = _mint_maintain_with_password(authed_app, "maintainer")
    headers = {"Authorization": f"Bearer {token}"}
    wrong = {"password": "not the password"}

    # `[security.sudo] max_failed_attempts` defaults to 5.
    for _ in range(5):
        authed_client.post("/auth/sudo", headers=headers, json=wrong)

    response = authed_client.post("/auth/sudo", headers=headers, json=wrong)

    assert response.status_code == 429
    assert response.json()["code"] == "ansina.auth.sudo_locked_out"
    assert "retry-after" in response.headers


def test_step_up_with_no_identity_is_401(client: TestClient) -> None:
    """`security.enabled = false` (the `client` fixture's dev mode) never resolves a
    `Principal` at all — there's no "who" to step up as.
    """
    response = client.post("/auth/sudo", json={"password": "anything"})

    assert response.status_code == 401
    assert response.json()["code"] == "ansina.unauthorized"


# --- DELETE /auth/sudo ----------------------------------------------------------------


def test_revoke_own_grant_invalidates_a_live_grant(
    authed_app: FastAPI, authed_client: TestClient
) -> None:
    token = _mint_maintain_with_password(authed_app, "maintainer")
    step_up = authed_client.post(
        "/auth/sudo",
        headers={"Authorization": f"Bearer {token}"},
        json={"password": _PASSWORD},
    )
    grant_token = step_up.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    revoke_response = authed_client.delete("/auth/sudo", headers=headers)
    assert revoke_response.status_code == 204

    # The revoked grant no longer elevates — the break-glass route now refuses.
    sensitive_response = authed_client.delete(
        "/auth/sudo/grants", headers={**headers, "X-Sudo-Token": grant_token}
    )
    assert sensitive_response.status_code == 403
    assert sensitive_response.json()["code"] == "ansina.auth.sudo_required"


def test_revoke_own_grant_with_no_identity_is_401(client: TestClient) -> None:
    response = client.delete("/auth/sudo")

    assert response.status_code == 401
    assert response.json()["code"] == "ansina.unauthorized"


# --- DELETE /auth/sudo/grants (break-glass) ------------------------------------------


def test_break_glass_requires_sudo_for_maintain(
    authed_app: FastAPI, authed_client: TestClient
) -> None:
    token = _mint_maintain_with_password(authed_app, "maintainer")

    response = authed_client.delete(
        "/auth/sudo/grants", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.auth.sudo_required"


def test_break_glass_succeeds_for_maintain_with_a_live_grant(
    authed_app: FastAPI, authed_client: TestClient
) -> None:
    db = authed_app.state.db
    token = _mint_maintain_with_password(authed_app, "maintainer")
    user = UserRepository(db).get_by_username("maintainer")
    assert user is not None
    SudoGrantRepository(db).create(
        user.id,
        "a-live-grant",
        "password",
        issued_at="2026-01-01T00:00:00.000Z",
        expires_at="2999-01-01T00:00:00.000Z",
    )

    response = authed_client.delete(
        "/auth/sudo/grants",
        headers={"Authorization": f"Bearer {token}", "X-Sudo-Token": "a-live-grant"},
    )

    assert response.status_code == 204


def test_break_glass_succeeds_for_admin_with_no_grant_at_all(
    authed_client: TestClient, authed_token: str
) -> None:
    """Admin never needs a sudo grant — by design, not an empty/always-valid one."""
    response = authed_client.delete(
        "/auth/sudo/grants", headers={"Authorization": f"Bearer {authed_token}"}
    )

    assert response.status_code == 204
