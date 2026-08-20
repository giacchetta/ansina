from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ansina.api.app import create_app
from ansina.config import load_settings

# Long enough to clear `SecuritySettings.api_token`'s `min_length=16`.
TEST_TOKEN = "unit-test-token-0123456789"


@pytest.fixture
def app(clean_env: None, tmp_cwd: Path) -> FastAPI:
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
