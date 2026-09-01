from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ansina.api.auth import _extract_bearer_token
from ansina.auth.models import CredentialType, SubjectType
from ansina.auth.repositories import (
    CredentialRepository,
    RoleAssignmentRepository,
    RoleRepository,
    UserRepository,
)


def test_extract_bearer_token_rejects_undecodable_bytes() -> None:
    # Mirrors `test_extract_inbound_id_rejects_undecodable_bytes` in test_middleware.py.
    scope = {"headers": [(b"authorization", b"\xff\xfe")]}
    assert _extract_bearer_token(scope) is None


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
