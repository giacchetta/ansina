from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ansina.api.app import create_app
from ansina.auth.hashing import Argon2Params
from ansina.auth.models import SubjectType
from ansina.auth.repositories import (
    CredentialRepository,
    RoleAssignmentRepository,
    RoleRepository,
    UserRepository,
)
from ansina.config import load_settings

# Long enough and high-entropy enough to clear `SecuritySettings.api_token`'s
# strength bar (>=32 chars, base64url charset, >=2.5 bits/char) — see
# `config/settings.py`'s `_TOKEN_MIN_LENGTH`/`_TOKEN_CHARSET`/
# `_TOKEN_MIN_ENTROPY_BITS_PER_CHAR`.
TEST_TOKEN = "unit-test-token-0123456789abcdefgh"


@pytest.fixture
def app(clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Auth disabled (`ANSINA_SECURITY__ENABLED=false`, default loopback host) — the
    "dev mode" fixture for tests that aren't about authentication itself. Since issue
    #24, an *unset* `api_token` no longer implies "no auth" (Ansina would instead
    auto-generate and enforce a bootstrap token) — dev mode has to be requested
    explicitly. See `authed_app`/`authed_client` for the auth-enforced counterpart.
    """
    monkeypatch.setenv("ANSINA_SECURITY__ENABLED", "false")
    return create_app(load_settings())


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def authed_token() -> str:
    return TEST_TOKEN


@pytest.fixture
def authed_app(
    clean_env: None,
    tmp_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
    authed_token: str,
) -> FastAPI:
    """Same as `app`, but with `ANSINA_SECURITY__API_TOKEN` set — auth enforced."""
    monkeypatch.setenv("ANSINA_SECURITY__API_TOKEN", authed_token)
    return create_app(load_settings())


@pytest.fixture
def authed_client(authed_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(authed_app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def token_for_role(authed_app: FastAPI) -> Callable[[str], str]:
    """A factory minting a fresh user + token holding exactly one builtin role, on
    `authed_app`'s own database — the shape `tests/unit/api/test_authorization.py`
    reuses for every "does role X get verb Y" case instead of hand-rolling the
    create-user/assign-role/create-token sequence per test.
    """
    counter = iter(range(1, 1_000))

    def _mint(role_slug: str) -> str:
        db = authed_app.state.db
        username = f"{role_slug}-user-{next(counter)}"
        token = f"{username}-token"
        user = UserRepository(db).create(username)
        role = RoleRepository(db).get_by_slug(role_slug)
        assert role is not None, f"unknown builtin role slug: {role_slug!r}"
        RoleAssignmentRepository(db).assign(SubjectType.USER, user.id, role.id)
        CredentialRepository(db).create_api_token(user.id, token)
        return token

    return _mint


_CHEAP_ARGON2 = Argon2Params(time_cost=1, memory_cost_kib=8, parallelism=1)
_SUDOED_MAINTAIN_PASSWORD = "correct horse battery staple"


@pytest.fixture
def sudoed_maintain(
    authed_app: FastAPI, authed_client: TestClient
) -> Callable[[], dict[str, str]]:
    """Mint a fresh `Maintain` user (password + api_token) and step it up, returning
    headers carrying both the bearer token and a live `X-Sudo-Token` grant — the shape
    every mutating `/auth/*` management route test needs (issue #27). Counter-suffixed
    so repeated calls within one test never collide.
    """
    counter = iter(range(1, 1_000))

    def _mint() -> dict[str, str]:
        db = authed_app.state.db
        username = f"maintainer-{next(counter)}"
        token = f"{username}-token"
        user = UserRepository(db).create(username)
        role = RoleRepository(db).get_by_slug("maintain")
        assert role is not None
        RoleAssignmentRepository(db).assign(SubjectType.USER, user.id, role.id)
        CredentialRepository(db).create_api_token(user.id, token)
        CredentialRepository(db).set_password(
            user.id, _SUDOED_MAINTAIN_PASSWORD, _CHEAP_ARGON2
        )
        headers = {"Authorization": f"Bearer {token}"}
        step_up = authed_client.post(
            "/auth/sudo", headers=headers, json={"password": _SUDOED_MAINTAIN_PASSWORD}
        )
        assert step_up.status_code == 200
        return {**headers, "X-Sudo-Token": step_up.json()["token"]}

    return _mint
