from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ansina.api.app import create_app
from ansina.auth.models import RoleSlug
from ansina.auth.repositories import (
    CredentialRepository,
    ExternalIdentityRepository,
    RoleAssignmentRepository,
    RoleRepository,
    UserRepository,
)
from ansina.brain.events import BrainDone
from ansina.brain.provider import BrainProvider, BrainRequest
from ansina.brain.selection import BrainUnavailableError
from ansina.config import Settings, load_settings
from ansina.errors import StorageError
from ansina.heart.runtime import BaseHeartRuntime, HeartRuntime, HeartUnavailableError
from ansina.heart.tick.loop import TickLifecycle, TickLoop
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


class _FakeBrainProvider:
    """Stands in for `OpenAICompatibleBrainProvider` so app-lifecycle tests never
    open a socket. Nothing calls `stream()` yet (issue #12 doesn't wire the tick
    loop's escalate branch to it) — only construction and `aclose()` matter here.
    """

    def __init__(self) -> None:
        self.close_calls = 0

    def stream(self, request: BrainRequest) -> AsyncGenerator[BrainDone]:
        async def _gen() -> AsyncGenerator[BrainDone]:
            yield BrainDone()

        return _gen()

    async def aclose(self) -> None:
        self.close_calls += 1


def test_app_state_carries_settings(app: FastAPI) -> None:
    assert isinstance(app.state.settings, Settings)


def test_app_state_carries_database(app: FastAPI) -> None:
    assert isinstance(app.state.db, Database)


def test_heart_disabled_by_default_no_state_no_readiness_key(app: FastAPI) -> None:
    assert app.state.heart is None
    assert app.state.tick_loop is None

    with TestClient(app) as client:
        checks = client.get("/readyz").json()["checks"]
        assert "heart" not in checks
        assert "heart_tick" not in checks


def test_heart_enabled_builds_loads_and_registers_readiness(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_HEART__ENABLED", "true")
    settings = load_settings()
    fake_heart = _FakeHeartRuntime()

    app = create_app(settings, heart_factory=lambda _settings: fake_heart)

    assert app.state.heart is fake_heart
    assert fake_heart.load_calls == 0  # not loaded until the lifespan runs
    assert isinstance(app.state.tick_loop, TickLoop)  # issue #11: on by default

    with TestClient(app) as client:
        assert fake_heart.load_calls == 1
        checks = client.get("/readyz").json()["checks"]
        assert checks["heart"] is True
        assert checks["heart_tick"] is True
        assert app.state.tick_loop.is_healthy()

    assert fake_heart.unload_calls == 1
    assert not app.state.tick_loop.is_healthy()  # stopped before heart.unload()


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


def test_tick_loop_factory_not_called_when_heart_disabled(
    clean_env: None, tmp_cwd: Path
) -> None:
    def _factory(_settings: Settings, _heart: HeartRuntime) -> TickLifecycle:
        pytest.fail("tick_loop_factory must not be called when heart.enabled is False")

    app = create_app(load_settings(), tick_loop_factory=_factory)

    assert app.state.tick_loop is None


def test_tick_loop_factory_not_called_when_tick_disabled(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_HEART__ENABLED", "true")
    monkeypatch.setenv("ANSINA_HEART__TICK__ENABLED", "false")

    def _factory(_settings: Settings, _heart: HeartRuntime) -> TickLifecycle:
        pytest.fail(
            "tick_loop_factory must not be called when heart.tick.enabled is False"
        )

    app = create_app(
        load_settings(),
        heart_factory=lambda _settings: _FakeHeartRuntime(),
        tick_loop_factory=_factory,
    )

    assert app.state.tick_loop is None

    with TestClient(app) as client:
        assert "heart_tick" not in client.get("/readyz").json()["checks"]


def test_tick_loop_started_after_heart_loads_and_stopped_before_heart_unloads(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_HEART__ENABLED", "true")
    settings = load_settings()
    events: list[str] = []

    class _OrderedHeart(_FakeHeartRuntime):
        def _load_backend(self) -> None:
            events.append("heart_load")
            super()._load_backend()

        def _unload_backend(self) -> None:
            events.append("heart_unload")
            super()._unload_backend()

    class _RecordingTickLoop:
        def is_healthy(self) -> bool:
            return True

        async def start(self) -> None:
            events.append("tick_start")

        async def stop(self) -> None:
            events.append("tick_stop")

    app = create_app(
        settings,
        heart_factory=lambda _settings: _OrderedHeart(),
        tick_loop_factory=lambda _settings, _heart: _RecordingTickLoop(),
    )

    with TestClient(app):
        pass

    assert events == ["heart_load", "tick_start", "tick_stop", "heart_unload"]


def test_brain_disabled_by_default_no_state_no_readiness_key(app: FastAPI) -> None:
    assert app.state.brain is None

    with TestClient(app) as client:
        checks = client.get("/readyz").json()["checks"]
        assert "brain" not in checks


def test_brain_enabled_builds_and_closes_on_shutdown(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_BRAIN__ENABLED", "true")
    settings = load_settings()
    fake_brain = _FakeBrainProvider()

    app = create_app(settings, brain_factory=lambda _settings: fake_brain)

    assert app.state.brain is fake_brain
    assert fake_brain.close_calls == 0  # not closed until the lifespan shuts down

    with TestClient(app):
        assert fake_brain.close_calls == 0

    assert fake_brain.close_calls == 1


def test_brain_factory_not_called_when_disabled(clean_env: None, tmp_cwd: Path) -> None:
    def _factory(_settings: Settings) -> BrainProvider:
        pytest.fail("brain_factory must not be called when brain.enabled is False")

    app = create_app(load_settings(), brain_factory=_factory)

    assert app.state.brain is None


def test_brain_unavailable_error_propagates_from_create_app(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_BRAIN__ENABLED", "true")
    settings = load_settings()

    def _factory(_settings: Settings) -> BrainProvider:
        raise BrainUnavailableError("no api_key configured")

    with pytest.raises(BrainUnavailableError):
        create_app(settings, brain_factory=_factory)


def test_brain_enabled_independent_of_heart(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Brain has no dependency on the Heart being enabled — issue #12 vs #10/#11
    are independent config surfaces."""
    monkeypatch.setenv("ANSINA_BRAIN__ENABLED", "true")
    settings = load_settings()

    app = create_app(settings, brain_factory=lambda _settings: _FakeBrainProvider())

    assert app.state.heart is None
    assert app.state.brain is not None


def test_lifespan_migrates_and_closes_the_database(app: FastAPI) -> None:
    with TestClient(app):
        # Inside the lifespan: migrated and usable.
        rows = (
            app.state.db.connection()
            .execute("SELECT version FROM schema_version")
            .fetchall()
        )
        assert [row[0] for row in rows] == [1, 2]

    # Outside the `with` block, lifespan shutdown has run — the database is closed.
    with pytest.raises(StorageError, match="after close"):
        app.state.db.connection()


def test_lifespan_seeds_builtin_roles_and_resources(app: FastAPI) -> None:
    """Issue #24: `sync_resources`/`reconcile_builtin_roles` run on every boot,
    independent of `security.enabled` (the `app` fixture has it disabled).
    """
    with TestClient(app):
        roles = RoleRepository(app.state.db)
        slugs = {r.slug for r in roles.list_all()}
        assert slugs == {slug.value for slug in RoleSlug}
        assert all(r.builtin for r in roles.list_all())


def test_lifespan_with_auth_disabled_creates_no_bootstrap_admin(app: FastAPI) -> None:
    """`app` (`ANSINA_SECURITY__ENABLED=false`) — dev mode needs no credential."""
    with TestClient(app):
        assert UserRepository(app.state.db).list_all() == []


def test_lifespan_with_auth_enabled_and_no_override_auto_generates_bootstrap_admin(
    clean_env: None, tmp_cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The production-default path (`security.enabled` at its `True` default, no
    `ANSINA_SECURITY__API_TOKEN` configured): Ansina generates its own bootstrap
    token, prints it once, and that token authenticates a real request through the
    fully-wired app — not just `ansina.auth.bootstrap` in isolation
    (`tests/unit/auth/test_bootstrap.py` covers that).
    """
    app = create_app(load_settings())
    with TestClient(app) as client:
        users = UserRepository(app.state.db).list_all()
        assert len(users) == 1

        output = capsys.readouterr().out
        match = re.search(r"^   (\S+)$", output, re.MULTILINE)
        assert match is not None, f"bootstrap token banner not found:\n{output}"
        token = match.group(1)

        response = client.get("/version", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200


def test_lifespan_with_an_api_token_creates_the_bootstrap_admin(
    authed_app: FastAPI, authed_token: str
) -> None:
    with TestClient(authed_app):
        users = UserRepository(authed_app.state.db).list_all()
        assert len(users) == 1
        identities = ExternalIdentityRepository(authed_app.state.db)
        identity = identities.get_by_provider_subject(
            "local-bootstrap", "bootstrap-admin"
        )
        assert identity is not None
        roles = RoleAssignmentRepository(authed_app.state.db).roles_for_user(
            users[0].id
        )
        assert [r.slug for r in roles] == [RoleSlug.ADMIN.value]
        found = CredentialRepository(authed_app.state.db).find_user_by_api_token(
            authed_token
        )
        assert found is not None
        assert found.id == users[0].id
