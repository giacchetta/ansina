from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ansina import __version__
from ansina.config import Settings
from ansina.errors import StorageError
from ansina.storage import Database


def test_app_state_carries_settings(app: FastAPI) -> None:
    assert isinstance(app.state.settings, Settings)


def test_app_state_carries_database(app: FastAPI) -> None:
    assert isinstance(app.state.db, Database)


def test_lifespan_migrates_and_closes_the_database(app: FastAPI) -> None:
    with TestClient(app):
        # Inside the lifespan: migrated and usable.
        rows = (
            app.state.db.connection()
            .execute("SELECT version FROM schema_version")
            .fetchall()
        )
        assert [row[0] for row in rows] == [1]

    # Outside the `with` block, lifespan shutdown has run — the database is closed.
    with pytest.raises(StorageError, match="after close"):
        app.state.db.connection()


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
