"""Unit tests for `GET /auth/me` (issue #30), `/auth/me/tokens` (issue #28),
`/auth/me/totp` (issue #41), and `/auth/me/password` (issue #48).

Reuses `tests/unit/api/conftest.py`'s `authed_client`/`token_for_role`/`sudoed_maintain`
fixtures — the same shape `test_sudo.py`/`test_permissions.py` already use. This
module overrides `authed_app`/`authed_client` to also configure
`[security.encryption] key`, which the TOTP tests need and every other test in this
file ignores.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ansina.api.app import create_app
from ansina.auth.clock import iso, utc_now
from ansina.auth.hashing import Argon2Params
from ansina.auth.models import RoleSlug
from ansina.auth.repositories import CredentialRepository, UserRepository
from ansina.auth.totp import totp_code
from ansina.config import load_settings

# A url-safe-base64, 32-raw-byte value — the shape `[security.encryption] key`
# requires (issue #41).
_ENCRYPTION_KEY = "inl1_UnlPfMEYIPwFZnl46Nx2GXZmHoHdT-OC4I9nYA"

_CHEAP_ARGON2 = Argon2Params(time_cost=1, memory_cost_kib=8, parallelism=1)

# Clears the default policy (`[security.password]`: min_length=12, max_length=1024,
# reject_common=True) and never contains any username `token_for_role` mints.
_ACCEPTABLE = "a truly excellent passphrase"


@pytest.fixture
def authed_app(
    clean_env: None,
    tmp_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
    authed_token: str,
    authed_admin_username: str,
) -> FastAPI:
    """Overrides `tests/unit/api/conftest.py`'s `authed_app` for this module only,
    adding `[security.encryption] key` — see the module docstring.
    """
    monkeypatch.setenv("ANSINA_SECURITY__ADMIN_USERNAME", authed_admin_username)
    monkeypatch.setenv("ANSINA_SECURITY__API_TOKEN", authed_token)
    monkeypatch.setenv("ANSINA_SECURITY__ENCRYPTION__KEY", _ENCRYPTION_KEY)
    return create_app(load_settings())


@pytest.fixture
def authed_client(authed_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(authed_app, raise_server_exceptions=False) as test_client:
        yield test_client


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


def test_step_up_factors_is_empty_for_a_password_less_caller(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    """Issue #37 AC: `GET /auth/me` reflects exactly the caller's enrolled
    credentials — `token_for_role` mints a user with an api_token only, no password.
    """
    token = token_for_role("read")

    response = authed_client.get(
        "/auth/me", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.json()["step_up_factors"] == []


def test_step_up_factors_lists_password_once_enrolled(
    authed_app: FastAPI, token_for_role: Callable[[str], str], authed_client: TestClient
) -> None:
    token = token_for_role("read")
    db = authed_app.state.db
    user = CredentialRepository(db).find_user_by_api_token(token, now=iso(utc_now()))
    assert user is not None
    CredentialRepository(db).set_password(
        user.id, "hunter2", Argon2Params(time_cost=1, memory_cost_kib=8, parallelism=1)
    )

    response = authed_client.get(
        "/auth/me", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.json()["step_up_factors"] == ["password"]


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


def test_mint_expires_at_is_null_for_the_http_route(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    """Issue #39 AC: `POST /auth/me/tokens` has no way to pass `ttl_seconds` (it isn't
    a field on `IssueTokenRequest`), so `expires_at` stays `null` end to end.
    """
    token = token_for_role("read")
    headers = {"Authorization": f"Bearer {token}"}

    response = authed_client.post("/auth/me/tokens", headers=headers, json={})

    assert response.status_code == 201
    assert response.json()["expires_at"] is None


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
    assert any(entry["expires_at"] is None for entry in body)
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


# --- /auth/me/totp: issue #41's self-service TOTP surface ----------------------------


@pytest.mark.parametrize("role", list(RoleSlug))
def test_every_builtin_role_can_enroll_check_and_disable_totp(
    authed_client: TestClient, token_for_role: Callable[[str], str], role: RoleSlug
) -> None:
    """The `me.*` carve-out again: every role reaches enroll/status with no 403 — but
    disabling needs a live sudo grant regardless of role (`Admin` excepted), so this
    only exercises `POST`/`GET` here; sudo-gating is its own test below.
    """
    token = token_for_role(role.value)
    headers = {"Authorization": f"Bearer {token}"}

    enrolled = authed_client.post("/auth/me/totp", headers=headers)
    assert enrolled.status_code == 201

    status = authed_client.get("/auth/me/totp", headers=headers)
    assert status.status_code == 200
    assert status.json()["enrolled"] is True


def test_enroll_returns_a_usable_secret_and_otpauth_uri(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("read")
    headers = {"Authorization": f"Bearer {token}"}

    response = authed_client.post("/auth/me/totp", headers=headers)

    assert response.status_code == 201
    body = response.json()
    assert body["secret"]
    assert body["otpauth_uri"].startswith("otpauth://totp/")
    assert body["digits"] == 6
    assert body["period_seconds"] == 30


def test_enroll_never_requires_sudo(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("maintain")
    headers = {"Authorization": f"Bearer {token}"}

    response = authed_client.post("/auth/me/totp", headers=headers)

    assert response.status_code == 201


def test_status_is_ungated_and_unenrolled_by_default(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("read")

    response = authed_client.get(
        "/auth/me/totp", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["enrolled"] is False
    assert body["enrolled_since"] is None


def test_status_reports_enrolled_since_the_enrollment_time(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("read")
    headers = {"Authorization": f"Bearer {token}"}
    authed_client.post("/auth/me/totp", headers=headers)

    response = authed_client.get("/auth/me/totp", headers=headers)

    assert response.status_code == 200
    assert response.json()["enrolled_since"]


def test_enrolling_twice_is_409(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("read")
    headers = {"Authorization": f"Bearer {token}"}
    authed_client.post("/auth/me/totp", headers=headers)

    response = authed_client.post("/auth/me/totp", headers=headers)

    assert response.status_code == 409
    assert response.json()["code"] == "ansina.auth.totp_already_enrolled"


def test_disable_requires_a_live_sudo_grant(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    """Issue #41 AC: a stolen bearer token alone must not be able to remove the
    caller's own second factor.
    """
    token = token_for_role("maintain")
    headers = {"Authorization": f"Bearer {token}"}
    authed_client.post("/auth/me/totp", headers=headers)

    response = authed_client.delete("/auth/me/totp", headers=headers)

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.auth.sudo_required"


def test_admin_can_disable_with_no_sudo_grant(
    authed_client: TestClient, authed_token: str
) -> None:
    headers = {"Authorization": f"Bearer {authed_token}"}
    authed_client.post("/auth/me/totp", headers=headers)

    response = authed_client.delete("/auth/me/totp", headers=headers)

    assert response.status_code == 204


def test_disable_is_idempotent_when_never_enrolled(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.delete(
        "/auth/me/totp", headers={"Authorization": f"Bearer {authed_token}"}
    )

    assert response.status_code == 204


def test_disable_with_a_live_grant_then_status_shows_unenrolled(
    authed_client: TestClient, sudoed_maintain: Callable[[], dict[str, str]]
) -> None:
    headers = sudoed_maintain()
    authed_client.post("/auth/me/totp", headers=headers)

    disabled = authed_client.delete("/auth/me/totp", headers=headers)
    assert disabled.status_code == 204

    status = authed_client.get("/auth/me/totp", headers=headers)
    assert status.json()["enrolled"] is False


def test_can_re_enroll_after_disabling(
    authed_client: TestClient, sudoed_maintain: Callable[[], dict[str, str]]
) -> None:
    headers = sudoed_maintain()
    authed_client.post("/auth/me/totp", headers=headers)
    authed_client.delete("/auth/me/totp", headers=headers)

    response = authed_client.post("/auth/me/totp", headers=headers)

    assert response.status_code == 201


def test_totp_routes_401_with_no_token(authed_client: TestClient) -> None:
    assert authed_client.post("/auth/me/totp").status_code == 401
    assert authed_client.get("/auth/me/totp").status_code == 401
    assert authed_client.delete("/auth/me/totp").status_code == 401


def test_totp_routes_have_no_identity_when_security_is_disabled(
    client: TestClient,
) -> None:
    for response in (
        client.post("/auth/me/totp"),
        client.get("/auth/me/totp"),
        client.delete("/auth/me/totp"),
    ):
        assert response.status_code == 401
        assert response.json()["code"] == "ansina.unauthorized"


def test_enrolled_secret_actually_verifies_against_sudo(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    """End-to-end proof the enrolled secret is the same one `POST /auth/sudo` checks
    against — not just that the HTTP shape looks right.
    """
    token = token_for_role("maintain")
    headers = {"Authorization": f"Bearer {token}"}
    enrolled = authed_client.post("/auth/me/totp", headers=headers).json()
    secret = _decode_base32(enrolled["secret"])
    code = totp_code(secret, at=int(utc_now().timestamp()))

    response = authed_client.post(
        "/auth/sudo", headers=headers, json={"code": code, "factor": "totp"}
    )

    assert response.status_code == 200
    assert response.json()["verifier"] == "totp"


def _decode_base32(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 8)
    return base64.b32decode(padded)


def test_enroll_fails_503_with_no_encryption_key_configured(
    clean_env: None,
    tmp_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
    authed_token: str,
    authed_admin_username: str,
) -> None:
    """Deliberately bypasses this module's own `authed_app` override (which always
    sets a key) to prove the request-time guard, not just the boot-time one.
    """
    monkeypatch.setenv("ANSINA_SECURITY__ADMIN_USERNAME", authed_admin_username)
    monkeypatch.setenv("ANSINA_SECURITY__API_TOKEN", authed_token)
    app = create_app(load_settings())

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/auth/me/totp", headers={"Authorization": f"Bearer {authed_token}"}
        )

    assert response.status_code == 503
    assert response.json()["code"] == "ansina.auth.encryption_key_missing"


# --- /auth/me/password: issue #48's self-service password change --------------------


@pytest.mark.parametrize("role", list(RoleSlug))
def test_every_builtin_role_can_set_its_first_password(
    authed_client: TestClient, token_for_role: Callable[[str], str], role: RoleSlug
) -> None:
    """The `me.*` carve-out again: `Read` can set its own password with no 403,
    unlike every `auth.*` route. `token_for_role` mints a user with no password
    credential, so this also exercises the first-password carve-out.
    """
    token = token_for_role(role.value)
    headers = {"Authorization": f"Bearer {token}"}

    response = authed_client.put(
        "/auth/me/password", headers=headers, json={"new_password": _ACCEPTABLE}
    )

    assert response.status_code == 204


def test_change_password_never_requires_sudo(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    """Issue #48 AC: `current_password` *is* the proof-of-possession — `Maintain`
    reaches this with no `X-Sudo-Token`, unlike every other `auth.*` mutation.
    """
    token = token_for_role("maintain")
    headers = {"Authorization": f"Bearer {token}"}

    response = authed_client.put(
        "/auth/me/password", headers=headers, json={"new_password": _ACCEPTABLE}
    )

    assert response.status_code == 204


def test_first_password_carve_out_ignores_a_submitted_current_password(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    """A caller with no password credential yet may include a (meaningless)
    `current_password` without it being checked against anything.
    """
    token = token_for_role("read")
    headers = {"Authorization": f"Bearer {token}"}

    response = authed_client.put(
        "/auth/me/password",
        headers=headers,
        json={"current_password": "anything at all", "new_password": _ACCEPTABLE},
    )

    assert response.status_code == 204


def test_changing_an_existing_password_requires_the_current_one(
    authed_app: FastAPI, authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("read")
    headers = {"Authorization": f"Bearer {token}"}
    db = authed_app.state.db
    user = CredentialRepository(db).find_user_by_api_token(token, now=iso(utc_now()))
    assert user is not None
    CredentialRepository(db).set_password(
        user.id, "the original password", _CHEAP_ARGON2
    )

    response = authed_client.put(
        "/auth/me/password",
        headers=headers,
        json={"current_password": "the original password", "new_password": _ACCEPTABLE},
    )

    assert response.status_code == 204
    assert CredentialRepository(db).verify_password(user.id, _ACCEPTABLE, _CHEAP_ARGON2)


def test_omitting_current_password_when_one_exists_is_401(
    authed_app: FastAPI, authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("read")
    headers = {"Authorization": f"Bearer {token}"}
    db = authed_app.state.db
    user = CredentialRepository(db).find_user_by_api_token(token, now=iso(utc_now()))
    assert user is not None
    CredentialRepository(db).set_password(
        user.id, "the original password", _CHEAP_ARGON2
    )

    response = authed_client.put(
        "/auth/me/password", headers=headers, json={"new_password": _ACCEPTABLE}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "ansina.unauthorized"
    # The stored credential is untouched — re-authenticating with the old password
    # still works (issue #48 AC).
    assert CredentialRepository(db).verify_password(
        user.id, "the original password", _CHEAP_ARGON2
    )


def test_wrong_current_password_is_401_and_leaves_the_credential_unchanged(
    authed_app: FastAPI, authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("read")
    headers = {"Authorization": f"Bearer {token}"}
    db = authed_app.state.db
    user = CredentialRepository(db).find_user_by_api_token(token, now=iso(utc_now()))
    assert user is not None
    CredentialRepository(db).set_password(
        user.id, "the original password", _CHEAP_ARGON2
    )

    response = authed_client.put(
        "/auth/me/password",
        headers=headers,
        json={"current_password": "totally wrong", "new_password": _ACCEPTABLE},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "ansina.unauthorized"
    assert CredentialRepository(db).verify_password(
        user.id, "the original password", _CHEAP_ARGON2
    )
    assert not CredentialRepository(db).verify_password(
        user.id, _ACCEPTABLE, _CHEAP_ARGON2
    )


def test_weak_new_password_is_400(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("read")
    headers = {"Authorization": f"Bearer {token}"}

    response = authed_client.put(
        "/auth/me/password", headers=headers, json={"new_password": "short1"}
    )

    assert response.status_code == 400
    assert response.json()["code"] == "ansina.auth.weak_password"


def test_new_password_can_then_be_used_to_step_up(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("maintain")
    headers = {"Authorization": f"Bearer {token}"}
    authed_client.put(
        "/auth/me/password", headers=headers, json={"new_password": _ACCEPTABLE}
    )

    step_up = authed_client.post(
        "/auth/sudo", headers=headers, json={"password": _ACCEPTABLE}
    )

    assert step_up.status_code == 200


def test_password_routes_401_with_no_token(authed_client: TestClient) -> None:
    response = authed_client.put(
        "/auth/me/password", json={"new_password": _ACCEPTABLE}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "ansina.unauthorized"


def test_password_route_has_no_identity_when_security_is_disabled(
    client: TestClient,
) -> None:
    response = client.put("/auth/me/password", json={"new_password": _ACCEPTABLE})

    assert response.status_code == 401
    assert response.json()["code"] == "ansina.unauthorized"
