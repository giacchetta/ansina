"""Tests for `api.routes.login` — `POST /auth/login`. See issue #50.

Covers the route layer: the one indistinguishable 401 across every failure mode, the
equalized-failure-timing discipline (a real argon2 verify runs on every path, proven
indirectly via `hashing.dummy_password_hash` rather than by timing wall-clock, which
would be flaky), the throttle (#49) wired in, the exact `expires_at` (#39), and that
`/auth/login` is genuinely public — reachable with no bearer token even when auth is
enforced, and contributes no `resources`/`GET /auth/permissions` entry.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest
from starlette.types import Scope

from ansina.api.app import create_app
from ansina.api.routes.login import _client_ip
from ansina.auth.clock import iso, utc_now
from ansina.auth.hashing import Argon2Params
from ansina.auth.login_throttle import LoginThrottle
from ansina.auth.models import SubjectType, User
from ansina.auth.repositories import (
    CredentialRepository,
    RoleAssignmentRepository,
    RoleRepository,
    UserRepository,
)
from ansina.config import load_settings
from ansina.config.settings import RESERVED_BOOTSTRAP_USERNAME, Settings
from ansina.storage.database import Database

_PASSWORD = "correct horse battery staple"
_CHEAP_ARGON2 = Argon2Params(time_cost=1, memory_cost_kib=8, parallelism=1)
# Comfortably past any real wall-clock this suite runs under — `expires_at` is
# computed from *this* fake clock, but `ApiTokenAuthenticator`'s own expiry check
# (issue #39) reads the *real* clock (`build_authenticators` takes no clock override),
# so a minted token must land in this fixture's future relative to real "now" or it
# would read back as already-expired the instant it's minted.
_START = datetime(2099, 1, 1, tzinfo=UTC)


class _Clock:
    """An injectable, manually-advanced clock — never real sleeping. Copied, not
    imported, from `tests/unit/auth/test_login_throttle.py`'s own `_Clock` helper —
    `tests/` has no `__init__.py`, so cross-file imports aren't reliable under this
    suite's `--import-mode=importlib`.
    """

    def __init__(self, start: datetime = _START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _create_user(
    db: Database, username: str, *, password: str | None = _PASSWORD
) -> User:
    """A user holding the `read` role (so a subsequent `GET /auth/me` isn't itself
    403 `ansina.forbidden` for holding no role at all — `POST /auth/users` always
    assigns one; this bypasses that route, so it must assign one itself), optionally
    with a `_PASSWORD` credential — `password=None` is the password-less shape
    `POST /auth/users` produces without `password` set.
    """
    user = UserRepository(db).create(username)
    read_role = RoleRepository(db).get_by_slug("read")
    assert read_role is not None
    RoleAssignmentRepository(db).assign(SubjectType.USER, user.id, read_role.id)
    if password is not None:
        CredentialRepository(db).set_password(user.id, password, _CHEAP_ARGON2)
    return user


def _build_login_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    admin_username: str,
    admin_token: str,
    clock: _Clock | None = None,
    extra_env: dict[str, str] | None = None,
) -> FastAPI:
    """Builds an auth-enforced app the same way `authed_app` does, plus (optionally)
    rewiring `app.state.login_throttle` onto an injected `_Clock` and/or extra env
    overrides — used by tests that need a lower throttle threshold or an exact,
    fake-clock-driven `expires_at`, neither of which `authed_app`'s own fixture
    composition can express (its env is resolved before a test body ever runs).
    """
    monkeypatch.setenv("ANSINA_SECURITY__ADMIN_USERNAME", admin_username)
    monkeypatch.setenv("ANSINA_SECURITY__API_TOKEN", admin_token)
    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)
    app = create_app(load_settings())
    if clock is not None:
        settings: Settings = app.state.settings
        app.state.login_throttle = LoginThrottle(
            app.state.db, settings.security.login, clock=clock
        )
    return app


@pytest.fixture
def login_clock() -> _Clock:
    return _Clock()


@pytest.fixture
def login_app(
    clean_env: None,
    tmp_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
    authed_token: str,
    authed_admin_username: str,
    login_clock: _Clock,
) -> FastAPI:
    return _build_login_app(
        monkeypatch,
        admin_username=authed_admin_username,
        admin_token=authed_token,
        clock=login_clock,
    )


@pytest.fixture
def login_client(login_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(login_app, raise_server_exceptions=False) as test_client:
        yield test_client


# --- success ----------------------------------------------------------------------


def test_valid_credentials_return_201_and_the_token_authenticates(
    login_client: TestClient, login_app: FastAPI
) -> None:
    _create_user(login_app.state.db, "someone")

    response = login_client.post(
        "/auth/login", json={"username": "someone", "password": _PASSWORD}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["label"] == "password login"
    assert body["expires_at"] is not None
    assert "hash" not in body
    assert "salt" not in body

    me = login_client.get(
        "/auth/me", headers={"Authorization": f"Bearer {body['token']}"}
    )
    assert me.status_code == 200
    assert me.json()["username"] == "someone"


def test_expires_at_is_exactly_now_plus_the_configured_ttl(
    login_client: TestClient, login_app: FastAPI, login_clock: _Clock
) -> None:
    _create_user(login_app.state.db, "ttl-user")
    settings: Settings = login_app.state.settings
    ttl = settings.security.login.token_ttl_seconds

    response = login_client.post(
        "/auth/login", json={"username": "ttl-user", "password": _PASSWORD}
    )

    assert response.status_code == 201
    expected = iso(login_clock.now + timedelta(seconds=ttl))
    assert response.json()["expires_at"] == expected


def test_minted_token_stops_authenticating_past_its_expiry(
    login_client: TestClient, login_app: FastAPI
) -> None:
    """#39's enforcement, proven end to end: the token works, then — once its stored
    `expires_at` is in the past — it doesn't. Manipulates the row directly rather than
    advancing a clock, since `ApiTokenAuthenticator`'s own clock (built inside
    `build_authenticators`) is real and not reachable from this fixture, only
    `LoginThrottle`'s is — the identical technique
    `tests/unit/auth/test_authenticator.py`'s own expired/deleted-user tests use.
    """
    _create_user(login_app.state.db, "expiring-user")
    minted = login_client.post(
        "/auth/login", json={"username": "expiring-user", "password": _PASSWORD}
    )
    assert minted.status_code == 201
    body = minted.json()
    token, token_id = body["token"], body["id"]

    still_valid = login_client.get(
        "/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert still_valid.status_code == 200

    with login_app.state.db.transaction() as cursor:
        cursor.execute(
            "UPDATE credentials SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00.000Z", token_id),
        )

    expired = login_client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert expired.status_code == 401


def test_security_disabled_still_mints_a_token_normally(
    app: FastAPI, client: TestClient
) -> None:
    """`security.enabled = false` never resolves a `Principal` — irrelevant here,
    since this route never reads one either way.
    """
    _create_user(app.state.db, "devmode-user")

    response = client.post(
        "/auth/login", json={"username": "devmode-user", "password": _PASSWORD}
    )

    assert response.status_code == 201
    assert response.json()["token"]


# --- the one 401 --------------------------------------------------------------------


def test_every_failure_mode_returns_the_byte_identical_401_body(
    login_client: TestClient, login_app: FastAPI
) -> None:
    """Unknown username, wrong password, a password-less user, an inactive user, a
    tombstoned user, and the bootstrap identity all produce the exact same
    `problem+json` body — compared to one another (modulo `request_id`), as the
    issue's own AC words it, not asserted six times independently.
    """
    db = login_app.state.db
    users = UserRepository(db)

    _create_user(db, "wrongpass-user")
    _create_user(db, "nopassword-user", password=None)
    inactive = _create_user(db, "inactive-user")
    users.set_active(inactive.id, active=False)
    tombstoned = _create_user(db, "tombstoned-user")
    users.soft_delete(tombstoned.id, deleted_at=iso(utc_now()))

    attempts = [
        {"username": "wrongpass-user", "password": "definitely-not-it"},
        {"username": "no-such-user-anywhere", "password": "whatever"},
        {"username": "nopassword-user", "password": "whatever"},
        {"username": "inactive-user", "password": _PASSWORD},
        {"username": "tombstoned-user", "password": _PASSWORD},
        {"username": RESERVED_BOOTSTRAP_USERNAME, "password": "whatever"},
    ]

    bodies = []
    for payload in attempts:
        response = login_client.post("/auth/login", json=payload)
        assert response.status_code == 401
        assert response.headers["content-type"] == "application/problem+json"
        body = response.json()
        body.pop("request_id", None)
        bodies.append(body)

    assert all(body == bodies[0] for body in bodies), bodies


# --- throttle (#49) ------------------------------------------------------------------


def test_repeated_failures_429_then_a_success_clears_both_buckets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_cwd: Path,
    clean_env: None,
    authed_token: str,
    authed_admin_username: str,
    login_clock: _Clock,
) -> None:
    app = _build_login_app(
        monkeypatch,
        admin_username=authed_admin_username,
        admin_token=authed_token,
        clock=login_clock,
        extra_env={"ANSINA_SECURITY__LOGIN__MAX_FAILED_ATTEMPTS_PER_USERNAME": "2"},
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        _create_user(app.state.db, "throttled-user")

        for _ in range(2):
            response = client.post(
                "/auth/login",
                json={"username": "throttled-user", "password": "wrong"},
            )
            assert response.status_code == 401

        locked = client.post(
            "/auth/login", json={"username": "throttled-user", "password": "wrong"}
        )
        assert locked.status_code == 429
        assert "retry-after" in locked.headers
        assert locked.json()["code"] == "ansina.auth.login_throttled"

        # Past the lockout window: a correct login now succeeds and clears both
        # buckets — proven by a fresh, single failure right after *not* re-locking,
        # which it would if the streak survived the success.
        login_clock.advance(app.state.settings.security.login.lockout_seconds + 1)
        ok = client.post(
            "/auth/login", json={"username": "throttled-user", "password": _PASSWORD}
        )
        assert ok.status_code == 201

        after_success = client.post(
            "/auth/login", json={"username": "throttled-user", "password": "wrong"}
        )
        assert after_success.status_code == 401


# --- public-route wiring -------------------------------------------------------------


def test_login_reachable_with_no_bearer_token_when_auth_is_enforced(
    authed_client: TestClient, authed_app: FastAPI
) -> None:
    """Proven positively (a real 201), not just by absence of a 401 — the same 401
    `code` (`ansina.unauthorized`) this route's own credential refusal produces would
    otherwise make "was I even let past the middleware" ambiguous.
    """
    _create_user(authed_app.state.db, "public-route-user")

    response = authed_client.post(
        "/auth/login", json={"username": "public-route-user", "password": _PASSWORD}
    )

    assert response.status_code == 201


def test_login_contributes_no_resource_catalog_entry(authed_app: FastAPI) -> None:
    names = {spec.name for spec in authed_app.state.resource_specs}
    assert not any("login" in name for name in names)


def test_permissions_endpoint_lists_no_login_resource(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.get(
        "/auth/permissions", headers={"Authorization": f"Bearer {authed_token}"}
    )

    assert response.status_code == 200
    resources = {entry["resource"] for entry in response.json()}
    assert not any("login" in resource for resource in resources)


# --- _client_ip ------------------------------------------------------------------


def test_client_ip_reads_the_real_client_host() -> None:
    scope: Scope = {
        "type": "http",
        "client": ("203.0.113.5", 12345),
        "headers": [],
        "query_string": b"",
        "path": "/auth/login",
        "method": "POST",
    }
    request = StarletteRequest(scope)

    assert _client_ip(request) == "203.0.113.5"


def test_client_ip_falls_back_for_a_clientless_scope() -> None:
    scope: Scope = {
        "type": "http",
        "client": None,
        "headers": [],
        "query_string": b"",
        "path": "/auth/login",
        "method": "POST",
    }
    request = StarletteRequest(scope)

    assert _client_ip(request) == "unknown"
