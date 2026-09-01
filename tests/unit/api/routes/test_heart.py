from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ansina.api.app import create_app
from ansina.config import Settings, load_settings
from ansina.heart.runtime import BaseHeartRuntime, HeartRuntime
from ansina.heart.tick.decision import TickDecision
from ansina.heart.tick.loop import TickLoop


class _FakeHeartRuntime(BaseHeartRuntime):
    """Mirrors `tests/unit/api/test_app.py`'s fake — no MLX, no real model."""

    def __init__(self) -> None:
        super().__init__(context_tokens=8192, max_output_tokens=512)

    def _load_backend(self) -> None:
        pass

    def _generate(self, prompt: str, max_tokens: int) -> str:
        return "idle"

    def _token_count(self, text: str) -> int:
        return len(text)

    def _unload_backend(self) -> None:
        pass


class _FakeTickLoop:
    """A `TickLoop`-shaped double, so route tests don't depend on real scheduling."""

    def __init__(self) -> None:
        self._paused = False
        self._started = False
        self._stopped = False
        self.ticks_run = 3
        self.last_decision: TickDecision | None = TickDecision.IDLE
        self.last_tick_at: str | None = "2026-01-01T00:00:00+00:00"
        self.last_duration_seconds: float | None = 0.012

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def is_healthy(self) -> bool:
        return self._started and not self._stopped

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._stopped = True


@pytest.fixture
def fake_tick_loop() -> _FakeTickLoop:
    return _FakeTickLoop()


@pytest.fixture
def heart_enabled_app(
    clean_env: None,
    tmp_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_tick_loop: _FakeTickLoop,
) -> FastAPI:
    # Auth disabled (dev mode) — this fixture is for exercising heart-tick route
    # behavior, not authentication (see `authed_client` for the deny-by-default
    # tests above). Since issue #24, an unset api_token no longer implies "no auth."
    monkeypatch.setenv("ANSINA_SECURITY__ENABLED", "false")
    monkeypatch.setenv("ANSINA_HEART__ENABLED", "true")
    settings = load_settings()
    return create_app(
        settings,
        heart_factory=lambda _settings: _FakeHeartRuntime(),
        tick_loop_factory=lambda _settings, _heart: fake_tick_loop,
    )


@pytest.fixture
def heart_enabled_client(heart_enabled_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(heart_enabled_app) as test_client:
        yield test_client


# --- deny-by-default: auth runs before route logic ----------------------------------


def test_get_tick_requires_auth_when_configured(authed_client: TestClient) -> None:
    response = authed_client.get("/heart/tick")

    assert response.status_code == 401


def test_pause_requires_auth_when_configured(authed_client: TestClient) -> None:
    response = authed_client.post("/heart/tick/pause")

    assert response.status_code == 401


# --- heart disabled: one clear, documented 503 --------------------------------------


def test_get_tick_503_when_heart_disabled(client: TestClient) -> None:
    response = client.get("/heart/tick")

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "ansina.heart.disabled"


def test_pause_503_when_heart_disabled(client: TestClient) -> None:
    response = client.post("/heart/tick/pause")

    assert response.status_code == 503
    assert response.json()["code"] == "ansina.heart.disabled"


def test_resume_503_when_heart_disabled(client: TestClient) -> None:
    response = client.post("/heart/tick/resume")

    assert response.status_code == 503
    assert response.json()["code"] == "ansina.heart.disabled"


# --- heart enabled: real status and control ------------------------------------------


def test_get_tick_status_reports_the_loop_state(
    heart_enabled_client: TestClient, fake_tick_loop: _FakeTickLoop
) -> None:
    response = heart_enabled_client.get("/heart/tick")

    assert response.status_code == 200
    body = response.json()
    assert body["running"] is True  # lifespan already called start()
    assert body["paused"] is False
    assert body["ticks"] == 3
    assert body["last_decision"] == "idle"
    assert body["last_tick_at"] == "2026-01-01T00:00:00+00:00"
    assert body["last_duration_seconds"] == 0.012


def test_pause_then_resume_round_trip(
    heart_enabled_client: TestClient, fake_tick_loop: _FakeTickLoop
) -> None:
    pause_response = heart_enabled_client.post("/heart/tick/pause")
    assert pause_response.status_code == 200
    assert pause_response.json() == {"paused": True}
    assert fake_tick_loop.paused
    assert heart_enabled_client.get("/heart/tick").json()["paused"] is True

    resume_response = heart_enabled_client.post("/heart/tick/resume")
    assert resume_response.status_code == 200
    assert resume_response.json() == {"paused": False}
    assert not fake_tick_loop.paused


def test_heart_factory_receives_real_heart_runtime(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`tick_loop_factory` is called with the same `HeartRuntime` `create_app` built,
    not a placeholder — this is what lets `build_tick_loop` wire `TickLoop` to it.
    """
    monkeypatch.setenv("ANSINA_HEART__ENABLED", "true")
    settings = load_settings()
    received: list[HeartRuntime] = []

    def _tick_loop_factory(_settings: Settings, heart: HeartRuntime) -> TickLoop:
        received.append(heart)
        return TickLoop(heart, interval_seconds=100, max_output_tokens=10)

    heart = _FakeHeartRuntime()
    create_app(
        settings,
        heart_factory=lambda _settings: heart,
        tick_loop_factory=_tick_loop_factory,
    )

    assert received == [heart]
