from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from ansina.api.auth import (
    BearerAuthMiddleware,
    _extract_bearer_token,
    _extract_sudo_token,
)
from ansina.api.exception_handlers import ansina_error_handler
from ansina.auth.models import CredentialType, SubjectType, User
from ansina.auth.principal import AuthMethod
from ansina.auth.repositories import (
    CredentialRepository,
    RoleAssignmentRepository,
    RoleRepository,
    SudoGrantRepository,
    UserRepository,
)
from ansina.errors import AnsinaError


def test_extract_bearer_token_rejects_undecodable_bytes() -> None:
    # Mirrors `test_extract_inbound_id_rejects_undecodable_bytes` in test_middleware.py.
    scope = {"headers": [(b"authorization", b"\xff\xfe")]}
    assert _extract_bearer_token(scope) is None


def test_extract_sudo_token_rejects_undecodable_bytes() -> None:
    scope = {"headers": [(b"x-sudo-token", b"\xff\xfe")]}
    assert _extract_sudo_token(scope) is None


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
def test_public_paths_reachable_without_token(
    path: str, authed_client: TestClient
) -> None:
    response = authed_client.get(path)

    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/version", "/openapi.json"])
def test_protected_paths_require_token(path: str, authed_client: TestClient) -> None:
    response = authed_client.get(path)

    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "ansina.unauthorized"
    assert response.headers["www-authenticate"] == "Bearer"


def test_correct_token_is_accepted(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.get(
        "/version", headers={"Authorization": f"Bearer {authed_token}"}
    )

    assert response.status_code == 200


def test_wrong_token_is_rejected(authed_client: TestClient, authed_token: str) -> None:
    response = authed_client.get(
        "/version", headers={"Authorization": f"Bearer {authed_token}x"}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "ansina.unauthorized"


@pytest.mark.parametrize(
    "scheme",
    ["Basic", "Token", "bearer", "BEARER"],
)
def test_non_bearer_or_odd_schemes(
    scheme: str, authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.get(
        "/version", headers={"Authorization": f"{scheme} {authed_token}"}
    )

    if scheme.strip().lower() == "bearer":
        assert response.status_code == 200  # scheme match is case-insensitive
    else:
        assert response.status_code == 401


@pytest.mark.parametrize(
    "header_value",
    [
        None,  # header absent entirely
        "",  # empty header
        "Bearer",  # scheme with no credential
        "just-the-token-no-scheme",
    ],
)
def test_malformed_or_missing_credentials_are_rejected(
    header_value: str | None, authed_client: TestClient
) -> None:
    headers = {"Authorization": header_value} if header_value is not None else {}
    response = authed_client.get("/version", headers=headers)

    assert response.status_code == 401
    assert response.json()["code"] == "ansina.unauthorized"


def test_auth_disabled_lets_every_route_through(client: TestClient) -> None:
    """`client`/`app` (`ANSINA_SECURITY__ENABLED=false`) — every route reachable."""
    for path in ("/healthz", "/readyz", "/version", "/openapi.json"):
        assert client.get(path).status_code == 200


def test_rejected_request_still_gets_a_request_id(authed_client: TestClient) -> None:
    """RequestIdMiddleware must stay outermost: even a 401 carries a request id."""
    response = authed_client.get("/version", headers={"X-Request-ID": "auth-trace"})

    assert response.status_code == 401
    assert response.headers["x-request-id"] == "auth-trace"
    assert response.json()["request_id"] == "auth-trace"


def test_rejected_request_is_still_access_logged(
    authed_client: TestClient,
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    response = authed_client.get("/version", headers={"X-Request-ID": "auth-log-trace"})

    assert response.status_code == 401
    logs = captured_logs()
    assert any(
        entry.get("request_id") == "auth-log-trace"
        and entry.get("extra", {}).get("status_code") == 401
        for entry in logs
    )


def test_verification_is_db_backed_not_a_single_static_secret(
    authed_app: FastAPI, authed_client: TestClient
) -> None:
    """Issue #24's redesign: `BearerAuthMiddleware` checks *any* active `credentials`
    row, not one static configured value — proven by minting a second user's token
    directly against the same database the running app is using and confirming it
    authenticates too, alongside the bootstrap token `authed_client` already proves.
    """
    db = authed_app.state.db
    user = UserRepository(db).create("second-user")
    role = RoleRepository(db).get_by_slug("admin")
    assert role is not None
    RoleAssignmentRepository(db).assign(SubjectType.USER, user.id, role.id)
    CredentialRepository(db).create_api_token(user.id, "a-second-users-own-token")

    response = authed_client.get(
        "/version", headers={"Authorization": "Bearer a-second-users-own-token"}
    )

    assert response.status_code == 200


def test_revoking_a_credential_rejects_its_token_on_the_next_request(
    authed_app: FastAPI, authed_client: TestClient, authed_token: str
) -> None:
    # Sanity: the token works before revocation.
    assert (
        authed_client.get(
            "/version", headers={"Authorization": f"Bearer {authed_token}"}
        ).status_code
        == 200
    )

    db = authed_app.state.db
    bootstrap_user = UserRepository(db).get_by_username("bootstrap-admin")
    assert bootstrap_user is not None
    CredentialRepository(db).delete_credentials(
        bootstrap_user.id, CredentialType.API_TOKEN
    )

    response = authed_client.get(
        "/version", headers={"Authorization": f"Bearer {authed_token}"}
    )

    assert response.status_code == 401


def test_an_inactive_users_token_no_longer_authenticates(
    authed_app: FastAPI, authed_client: TestClient
) -> None:
    """Issue #25: `resolve_principal` rejects an inactive user's token even though
    `find_user_by_api_token` itself doesn't filter on `users.active` — the one
    deliberate addition beyond formalizing #24's lookup.
    """
    db = authed_app.state.db
    user = UserRepository(db).create("disabled-user")
    role = RoleRepository(db).get_by_slug("admin")
    assert role is not None
    RoleAssignmentRepository(db).assign(SubjectType.USER, user.id, role.id)
    CredentialRepository(db).create_api_token(user.id, "disabled-user-token")
    UserRepository(db).set_active(user.id, active=False)

    response = authed_client.get(
        "/version", headers={"Authorization": "Bearer disabled-user-token"}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "ansina.unauthorized"


def test_a_read_role_user_gets_403_on_a_mutating_route_before_route_logic_runs(
    authed_app: FastAPI, authed_client: TestClient
) -> None:
    """Authorization (issue #25) runs as a route dependency, ahead of the handler
    body — a `Read`-role caller gets 403 on `POST /heart/tick/pause` even though the
    Heart is disabled (which would otherwise 503), because it never reaches that code.
    """
    db = authed_app.state.db
    user = UserRepository(db).create("reader")
    role = RoleRepository(db).get_by_slug("read")
    assert role is not None
    RoleAssignmentRepository(db).assign(SubjectType.USER, user.id, role.id)
    CredentialRepository(db).create_api_token(user.id, "reader-token")

    response = authed_client.post(
        "/heart/tick/pause", headers={"Authorization": "Bearer reader-token"}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.forbidden"


def test_middleware_authenticators_param_is_real_dependency_injection(
    authed_app: FastAPI, authed_client: TestClient
) -> None:
    """The `authenticators` constructor param (issue #25's formalized chain) is
    exercised end to end: a tiny standalone app wired with one custom `Authenticator`
    (never touching `ApiTokenAuthenticator`) authenticates a token only that member
    recognizes, and the resulting `Principal` lands on `request.state` for a handler
    to read — proving `BearerAuthMiddleware` now runs through the chain, not an
    inline lookup of its own.
    """
    db = authed_app.state.db
    user = UserRepository(db).create("custom-chain-user")

    class _AlwaysMatchAuthenticator:
        method = AuthMethod.API_TOKEN

        def authenticate(self, credential: str) -> User | None:
            return user if credential == "custom-secret" else None

    app = FastAPI()
    app.add_middleware(
        BearerAuthMiddleware,
        enabled=True,
        db=db,
        authenticators=(_AlwaysMatchAuthenticator(),),
    )
    app.add_exception_handler(AnsinaError, ansina_error_handler)

    @app.get("/ping")
    async def ping(request: Request) -> dict[str, str]:
        return {"actor": request.state.principal.actor}

    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/ping", headers={"Authorization": "Bearer custom-secret"})

    assert response.status_code == 200
    assert response.json() == {"actor": "custom-chain-user"}


def test_valid_sudo_token_elevates_and_reaches_a_sensitive_route(
    authed_app: FastAPI, authed_client: TestClient
) -> None:
    """Issue #26: `BearerAuthMiddleware` elevates a resolved `Principal` when a
    live `X-Sudo-Token` accompanies it — proven against the real `DELETE /auth/sudo/
    grants` sensitive route rather than a probe, so this also exercises `require(...,
    sensitive=True)`'s own check end to end. The grant is seeded directly via
    `SudoGrantRepository`, not `POST /auth/sudo` — issuance itself is `test_sudo.py`'s
    and `test_routes/test_sudo.py`'s job; this test is only about the middleware
    reading a grant back.
    """
    db = authed_app.state.db
    user = UserRepository(db).create("maintainer")
    role = RoleRepository(db).get_by_slug("maintain")
    assert role is not None
    RoleAssignmentRepository(db).assign(SubjectType.USER, user.id, role.id)
    CredentialRepository(db).create_api_token(user.id, "maintainer-token")
    SudoGrantRepository(db).create(
        user.id,
        "a-live-sudo-grant",
        "password",
        issued_at="2026-01-01T00:00:00.000Z",
        expires_at="2999-01-01T00:00:00.000Z",
    )

    response = authed_client.delete(
        "/auth/sudo/grants",
        headers={
            "Authorization": "Bearer maintainer-token",
            "X-Sudo-Token": "a-live-sudo-grant",
        },
    )

    assert response.status_code == 204


def test_a_wrong_sudo_token_does_not_elevate_and_stays_sudo_required(
    authed_app: FastAPI, authed_client: TestClient
) -> None:
    """An unrecognized `X-Sudo-Token` deliberately never becomes a 401 by itself — the
    caller's bearer token is still perfectly valid, it just fails to elevate, so a
    sensitive route answers its own 403 `CODE_SUDO_REQUIRED`.
    """
    db = authed_app.state.db
    user = UserRepository(db).create("maintainer-2")
    role = RoleRepository(db).get_by_slug("maintain")
    assert role is not None
    RoleAssignmentRepository(db).assign(SubjectType.USER, user.id, role.id)
    CredentialRepository(db).create_api_token(user.id, "maintainer-2-token")

    response = authed_client.delete(
        "/auth/sudo/grants",
        headers={
            "Authorization": "Bearer maintainer-2-token",
            "X-Sudo-Token": "not-a-real-grant",
        },
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.auth.sudo_required"
