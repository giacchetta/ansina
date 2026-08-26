from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ansina.api.app import create_app
from ansina.config import Settings, load_settings
from ansina.errors import StorageError
from ansina.heart.runtime import BaseHeartRuntime, HeartUnavailableError
from ansina.storage import Database


class _FakeHeartRuntime(BaseHeartRuntime):
    """Stands in for `MlxHeartRuntime` so app-lifecycle tests never need MLX or a
    real model — mirrors `tests/unit/heart/test_runtime.py`'s fake.
    """

    def __init__(self) -> None:
        super().__init__(context_tokens=8192, max_output_tokens=512)
        self.load_calls = 0
        self.unload_calls = 0

    def _load_backend(self) -> None:
        self.load_calls += 1

    def _generate(self, prompt: str, max_tokens: int) -> str:
        return "generated"

    def _token_count(self, text: str) -> int:
        return len(text)

    def _unload_backend(self) -> None:
        self.unload_calls += 1


def test_app_state_carries_settings(app: FastAPI) -> None:
    assert isinstance(app.state.settings, Settings)


def test_app_state_carries_database(app: FastAPI) -> None:
    assert isinstance(app.state.db, Database)


def test_heart_disabled_by_default_no_state_no_readiness_key(app: FastAPI) -> None:
    assert app.state.heart is None

    with TestClient(app) as client:
        assert "heart" not in client.get("/readyz").json()["checks"]


def test_heart_enabled_builds_loads_and_registers_readiness(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_HEART__ENABLED", "true")
    settings = load_settings()
    fake_heart = _FakeHeartRuntime()

    app = create_app(settings, heart_factory=lambda _settings: fake_heart)

    assert app.state.heart is fake_heart
    assert fake_heart.load_calls == 0  # not loaded until the lifespan runs

    with TestClient(app) as client:
        assert fake_heart.load_calls == 1
        assert client.get("/readyz").json()["checks"]["heart"] is True

    assert fake_heart.unload_calls == 1


def test_heart_factory_not_called_when_disabled(clean_env: None, tmp_cwd: Path) -> None:
    def _factory(_settings: Settings) -> BaseHeartRuntime:
        pytest.fail("heart_factory must not be called when heart.enabled is False")

    create_app(load_settings(), heart_factory=_factory)


def test_heart_unavailable_error_propagates_from_create_app(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_HEART__ENABLED", "true")
    settings = load_settings()

    def _factory(_settings: Settings) -> BaseHeartRuntime:
        raise HeartUnavailableError("no viable adapter")

    with pytest.raises(HeartUnavailableError):
        create_app(settings, heart_factory=_factory)


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
