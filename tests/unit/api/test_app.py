from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ansina import __version__
from ansina.config import Settings


def test_app_state_carries_settings(app: FastAPI) -> None:
    assert isinstance(app.state.settings, Settings)


def test_healthz_ok_with_no_dependencies(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version_matches_package_version(client: TestClient) -> None:
    response = client.get("/version")

    assert response.status_code == 200
    assert response.json() == {"name": "ansina", "version": __version__}


def test_openapi_schema_is_served_and_lists_all_routes(client: TestClient) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert set(paths) == {"/healthz", "/readyz", "/version"}
