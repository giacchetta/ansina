from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
