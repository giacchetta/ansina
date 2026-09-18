"""Tests for `api.routes.oidc` — `POST /auth/oidc/login` + `GET /auth/oidc/callback`.
See issue #43.

`test_oidc.py`/`test_oidc_login.py` already cover the login-exchange logic (ID-token
rejection cases, provisioning/linking, the AC #4 role-mapping refresh) in depth; this
file is about the route layer itself: disabled-503, `PUBLIC_PATHS` reachability
end to end, query-param validation, status-code/problem-body mapping, and that the
route actually mints an `api_token` (`ansina.api.tokens.issue_token`) with the
service's own clock/TTL rather than a second, independent one.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ansina.api.app import create_app
from ansina.auth.oidc import OidcHttpClient
from ansina.auth.oidc_login import OidcLoginService
from ansina.auth.repositories import CredentialRepository, UserRepository
from ansina.config import load_settings
from ansina.config.settings import Settings
from ansina.storage.database import Database

FakeClientFactory = Callable[..., OidcHttpClient]


@pytest.fixture
def oidc_enabled_app(
    clean_env: None,
    tmp_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_idp_client_factory: FakeClientFactory,
    idp_jwks: dict[str, object],
    oidc_issuer: str,
    oidc_client_id: str,
) -> FastAPI:
    """Auth disabled (dev mode) — these routes are public regardless (proven
    separately in `tests/unit/api/test_auth.py`/`test_app.py`), so this fixture is
    about exercising the OIDC routes themselves, not authentication. `oidc_factory`
    swaps in a fake IdP client so no network is ever touched.
    """
    monkeypatch.setenv("ANSINA_SECURITY__ENABLED", "false")
    monkeypatch.setenv("ANSINA_SECURITY__OIDC__ENABLED", "true")
    monkeypatch.setenv("ANSINA_SECURITY__OIDC__ISSUER", oidc_issuer)
    monkeypatch.setenv("ANSINA_SECURITY__OIDC__CLIENT_ID", oidc_client_id)
    monkeypatch.setenv("ANSINA_SECURITY__OIDC__CLIENT_SECRET", "route-test-secret")
    monkeypatch.setenv(
        "ANSINA_SECURITY__OIDC__REDIRECT_URI", "https://ansina.test/auth/oidc/callback"
    )
    settings = load_settings()
    client = fake_idp_client_factory(issuer=oidc_issuer, jwks=idp_jwks)

    def _factory(db: Database, factory_settings: Settings) -> OidcLoginService:
        return OidcLoginService(db, factory_settings.security.oidc, client=client)

    return create_app(settings, oidc_factory=_factory)


@pytest.fixture
def oidc_enabled_client(oidc_enabled_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(oidc_enabled_app) as test_client:
        yield test_client


def _nonce_from(authorization_url: str) -> str:
    return parse_qs(urlparse(authorization_url).query)["nonce"][0]


# --- disabled -------------------------------------------------------------------------


def test_login_503_when_disabled(client: TestClient) -> None:
    response = client.post("/auth/oidc/login")

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "ansina.auth.oidc_disabled"


def test_callback_503_when_disabled(client: TestClient) -> None:
    response = client.get("/auth/oidc/callback", params={"code": "x", "state": "y"})

    assert response.status_code == 503
    assert response.json()["code"] == "ansina.auth.oidc_disabled"


# --- POST /auth/oidc/login ------------------------------------------------------------


def test_login_returns_authorization_url_state_and_expiry(
    oidc_enabled_client: TestClient,
) -> None:
    response = oidc_enabled_client.post("/auth/oidc/login")

    assert response.status_code == 200
    body = response.json()
    assert body["authorization_url"].startswith("https://idp.test.example/authorize?")
    assert body["state"]
    assert body["expires_at"]


# --- GET /auth/oidc/callback: query-param validation ----------------------------------


def test_callback_400_when_idp_reports_an_error(
    oidc_enabled_client: TestClient,
) -> None:
    login = oidc_enabled_client.post("/auth/oidc/login").json()

    response = oidc_enabled_client.get(
        "/auth/oidc/callback",
        params={"state": login["state"], "error": "access_denied"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "ansina.auth.oidc_callback_failed"


@pytest.mark.parametrize(
    "params",
    [
        {"state": "some-state"},  # missing code
        {"code": "some-code"},  # missing state
    ],
)
def test_callback_400_when_code_or_state_is_missing(
    oidc_enabled_client: TestClient, params: dict[str, str]
) -> None:
    response = oidc_enabled_client.get("/auth/oidc/callback", params=params)

    assert response.status_code == 400
    assert response.json()["code"] == "ansina.auth.oidc_callback_failed"


def test_callback_400_on_an_unknown_state(oidc_enabled_client: TestClient) -> None:
    response = oidc_enabled_client.get(
        "/auth/oidc/callback",
        params={"code": "some-code", "state": "never-issued-state"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "ansina.auth.oidc_state_invalid"


# --- GET /auth/oidc/callback: full round trip -----------------------------------------


def test_callback_round_trip_mints_a_token(
    oidc_enabled_app: FastAPI,
    oidc_enabled_client: TestClient,
    fake_idp_client_factory: FakeClientFactory,
    idp_jwks: dict[str, object],
    oidc_issuer: str,
    sign_id_token: Callable[..., str],
) -> None:
    login = oidc_enabled_client.post("/auth/oidc/login").json()
    nonce = _nonce_from(login["authorization_url"])

    id_token = sign_id_token(
        sub="route-test-subject",
        nonce=nonce,
        extra_claims={"preferred_username": "route-test-user"},
    )
    oidc_enabled_app.state.oidc._client = fake_idp_client_factory(
        issuer=oidc_issuer, jwks=idp_jwks, token_response={"id_token": id_token}
    )

    response = oidc_enabled_client.get(
        "/auth/oidc/callback", params={"code": "auth-code", "state": login["state"]}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["label"] == "oidc login"
    assert body["expires_at"] is not None
    assert body["token"]
    assert "hash" not in body
    assert "salt" not in body

    db = oidc_enabled_app.state.db
    user = UserRepository(db).get_by_username("route-test-user")
    assert user is not None
    tokens = CredentialRepository(db).list_api_tokens(user.id)
    assert len(tokens) == 1
    assert tokens[0].expires_at is not None


def test_callback_replay_is_rejected(
    oidc_enabled_app: FastAPI,
    oidc_enabled_client: TestClient,
    fake_idp_client_factory: FakeClientFactory,
    idp_jwks: dict[str, object],
    oidc_issuer: str,
    sign_id_token: Callable[..., str],
) -> None:
    login = oidc_enabled_client.post("/auth/oidc/login").json()
    nonce = _nonce_from(login["authorization_url"])
    id_token = sign_id_token(sub="replay-subject", nonce=nonce)
    oidc_enabled_app.state.oidc._client = fake_idp_client_factory(
        issuer=oidc_issuer, jwks=idp_jwks, token_response={"id_token": id_token}
    )
    params = {"code": "auth-code", "state": login["state"]}
    first = oidc_enabled_client.get("/auth/oidc/callback", params=params)
    assert first.status_code == 200

    second = oidc_enabled_client.get("/auth/oidc/callback", params=params)

    assert second.status_code == 400
    assert second.json()["code"] == "ansina.auth.oidc_state_invalid"


def test_callback_401_on_a_tampered_token(
    oidc_enabled_app: FastAPI,
    oidc_enabled_client: TestClient,
    fake_idp_client_factory: FakeClientFactory,
    idp_jwks: dict[str, object],
    oidc_issuer: str,
    sign_id_token: Callable[..., str],
) -> None:
    login = oidc_enabled_client.post("/auth/oidc/login").json()
    # Deliberately the wrong nonce — the id_token won't match this login's own.
    id_token = sign_id_token(sub="tampered-subject", nonce="not-this-logins-nonce")
    oidc_enabled_app.state.oidc._client = fake_idp_client_factory(
        issuer=oidc_issuer, jwks=idp_jwks, token_response={"id_token": id_token}
    )

    response = oidc_enabled_client.get(
        "/auth/oidc/callback", params={"code": "auth-code", "state": login["state"]}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "ansina.auth.oidc_token_invalid"


def test_callback_502_when_the_idp_is_unreachable(
    oidc_enabled_app: FastAPI,
    oidc_enabled_client: TestClient,
    unreachable_client: OidcHttpClient,
) -> None:
    login = oidc_enabled_client.post("/auth/oidc/login").json()
    # The login above already succeeded against the working fake client (which built
    # `oidc_enabled_app`'s discovery/JWKS); now swap in one that fails every call, to
    # prove a mid-flow provider outage surfaces as 502, not a bare 500.
    oidc_enabled_app.state.oidc._client = unreachable_client

    response = oidc_enabled_client.get(
        "/auth/oidc/callback", params={"code": "auth-code", "state": login["state"]}
    )

    assert response.status_code == 502
    assert response.json()["code"] == "ansina.auth.oidc_provider_unavailable"
