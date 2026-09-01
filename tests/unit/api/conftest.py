from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ansina.api.app import create_app
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
