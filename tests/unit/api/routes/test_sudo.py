from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ansina.api.app import create_app
from ansina.auth.clock import utc_now
from ansina.auth.encryption import encrypt
from ansina.auth.hashing import Argon2Params
from ansina.auth.models import SubjectType
from ansina.auth.repositories import (
    CredentialRepository,
    RoleAssignmentRepository,
    RoleRepository,
    SudoGrantRepository,
    UserRepository,
)
from ansina.auth.totp import totp_code
from ansina.config import load_settings

_CHEAP_ARGON2 = Argon2Params(time_cost=1, memory_cost_kib=8, parallelism=1)
_PASSWORD = "correct horse battery staple"

# A url-safe-base64, 32-raw-byte value — the shape `[security.encryption] key`
# requires (issue #41). Only the TOTP tests below need this; every password-only test
# in this file is unaffected by it being configured.
_ENCRYPTION_KEY = "inl1_UnlPfMEYIPwFZnl46Nx2GXZmHoHdT-OC4I9nYA"
# What `_ENCRYPTION_KEY` itself decodes to — the raw bytes `TotpStepUpVerifier`
# actually encrypts/decrypts with once `resolve_key` reads the env var above.
_ENCRYPTION_KEY_BYTES = base64.urlsafe_b64decode(_ENCRYPTION_KEY + "=")
_TOTP_SECRET = b"1" * 20


@pytest.fixture
def authed_app(
    clean_env: None,
    tmp_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
    authed_token: str,
    authed_admin_username: str,
) -> FastAPI:
    """Overrides `tests/unit/api/conftest.py`'s `authed_app` for this module only,
    adding `[security.encryption] key` (issue #41) — TOTP step-up tests below need a
    real key configured; every other test in this file ignores the extra setting.
    """
    monkeypatch.setenv("ANSINA_SECURITY__ADMIN_USERNAME", authed_admin_username)
    monkeypatch.setenv("ANSINA_SECURITY__API_TOKEN", authed_token)
    monkeypatch.setenv("ANSINA_SECURITY__ENCRYPTION__KEY", _ENCRYPTION_KEY)
    return create_app(load_settings())


@pytest.fixture
def authed_client(authed_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(authed_app, raise_server_exceptions=False) as test_client:
        yield test_client


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


def _mint_maintain_with_totp(authed_app: FastAPI, username: str) -> str:
    """A fresh `Maintain` user with an api_token and a TOTP enrollment (`_TOTP_SECRET`,
    encrypted under `_ENCRYPTION_KEY_BYTES` — the raw bytes `_ENCRYPTION_KEY` itself
    decodes to) — no password at all, the population issue #41 exists for.
    """
    db = authed_app.state.db
    user = UserRepository(db).create(username)
    role = RoleRepository(db).get_by_slug("maintain")
    assert role is not None
    RoleAssignmentRepository(db).assign(SubjectType.USER, user.id, role.id)
    token = f"{username}-token"
    CredentialRepository(db).create_api_token(user.id, token)
    CredentialRepository(db).create_totp_secret(
        user.id, encrypt(_TOTP_SECRET, _ENCRYPTION_KEY_BYTES)
    )
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


# --- POST /auth/sudo: no usable factor (issue #37) -----------------------------------


def test_password_less_maintain_gets_step_up_unavailable_not_401(
    authed_app: FastAPI,
    authed_client: TestClient,
    token_for_role: Callable[[str], str],
) -> None:
    """A `Maintain` user minted with no password (`token_for_role` never sets one) has
    zero enrolled step-up factors — 403 `ansina.auth.step_up_unavailable`, not a
    misleading 401, and the lockout counter is never touched.
    """
    token = token_for_role("maintain")

    response = authed_client.post(
        "/auth/sudo",
        headers={"Authorization": f"Bearer {token}"},
        json={"password": "anything"},
    )

    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "ansina.auth.step_up_unavailable"
    assert body["available_factors"] == []


def test_step_up_unavailable_never_locks_the_caller_out(
    authed_app: FastAPI,
    authed_client: TestClient,
    token_for_role: Callable[[str], str],
) -> None:
    token = token_for_role("maintain")
    headers = {"Authorization": f"Bearer {token}"}

    for _ in range(10):
        response = authed_client.post(
            "/auth/sudo", headers=headers, json={"password": "anything"}
        )
        assert response.status_code == 403
        assert response.json()["code"] == "ansina.auth.step_up_unavailable"


def test_step_up_with_no_identity_is_401(client: TestClient) -> None:
    """`security.enabled = false` (the `client` fixture's dev mode) never resolves a
    `Principal` at all — there's no "who" to step up as.
    """
    response = client.post("/auth/sudo", json={"password": "anything"})

    assert response.status_code == 401
    assert response.json()["code"] == "ansina.unauthorized"


# --- POST /auth/sudo: TOTP factor (issue #41) -----------------------------------------


def test_correct_totp_code_issues_a_grant(
    authed_app: FastAPI, authed_client: TestClient
) -> None:
    """Issue #41's headline AC: a user with no password and no external identity can
    still clear a `sensitive=True` gate, using a valid TOTP code. The code is computed
    against the real wall clock (`TotpStepUpVerifier`'s own default) rather than an
    injected fake — `find_valid_step`'s ±1-step drift window is generous enough that
    computing it immediately before the request is reliable.
    """
    token = _mint_maintain_with_totp(authed_app, "maintainer")
    code = totp_code(_TOTP_SECRET, at=int(utc_now().timestamp()))

    response = authed_client.post(
        "/auth/sudo",
        headers={"Authorization": f"Bearer {token}"},
        json={"code": code, "factor": "totp"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["verifier"] == "totp"
    assert isinstance(body["token"], str) and body["token"]


def test_wrong_totp_code_is_401(authed_app: FastAPI, authed_client: TestClient) -> None:
    token = _mint_maintain_with_totp(authed_app, "maintainer")

    response = authed_client.post(
        "/auth/sudo",
        headers={"Authorization": f"Bearer {token}"},
        json={"code": "000000", "factor": "totp"},
    )

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
