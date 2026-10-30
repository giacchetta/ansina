from __future__ import annotations

from collections.abc import Iterator
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
from ansina.config import Settings, load_settings
from ansina.opencode.events import RunOutcome, RunRequest
from ansina.opencode.events import RunStatus as OpencodeRunStatus
from ansina.opencode.repository import OpencodeRunRepository
from ansina.opencode.runner import OnEvent
from ansina.opencode.service import OpencodeRunService
from ansina.opencode.telemetry import OpencodeTelemetryWriter
from ansina.storage.database import Database
from ansina.telemetry.rotation import SpooledRotatingWriter

_ADMIN_USERNAME = "opencode-intake-admin"
_ADMIN_TOKEN = "opencode-intake-admin-token-0123456789a"
_CHEAP_ARGON2 = Argon2Params(time_cost=1, memory_cost_kib=8, parallelism=1)
_MAINTAIN_PASSWORD = "correct horse battery staple intake"


class _FakeAgentRunner:
    """Mirrors `test_opencode_runs.py`'s own fake exactly."""

    async def run(self, request: RunRequest, on_event: OnEvent) -> RunOutcome:
        return RunOutcome(
            status=OpencodeRunStatus.COMPLETED,
            exit_code=0,
            events_emitted=0,
            events_skipped=0,
            duration_seconds=0.1,
        )


@pytest.fixture
def opencode_app(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> FastAPI:
    monkeypatch.setenv("ANSINA_SECURITY__ADMIN_USERNAME", _ADMIN_USERNAME)
    monkeypatch.setenv("ANSINA_SECURITY__API_TOKEN", _ADMIN_TOKEN)
    monkeypatch.setenv("ANSINA_OPENCODE__ENABLED", "true")
    monkeypatch.setenv("ANSINA_OPENCODE__WORKSPACE_DIR", str(tmp_cwd / "workspace"))
    monkeypatch.setenv("ANSINA_TELEMETRY__ENABLED", "true")
    monkeypatch.setenv("ANSINA_TELEMETRY__SPOOL_DIR", str(tmp_cwd / "spool"))
    settings = load_settings()

    def _factory(settings: Settings, *, db: Database) -> OpencodeRunService:
        writer = SpooledRotatingWriter(
            spool_dir=settings.telemetry.spool_dir,
            file_prefix="opencode",
            max_file_bytes=settings.telemetry.max_file_bytes,
            max_spool_bytes=settings.telemetry.max_spool_bytes,
            retention_hours=settings.telemetry.retention_hours,
        )
        telemetry = OpencodeTelemetryWriter(
            writer=writer, max_line_bytes=settings.opencode.max_event_bytes
        )
        return OpencodeRunService(
            runner=_FakeAgentRunner(),
            repository=OpencodeRunRepository(db),
            telemetry=telemetry,
            settings=settings.opencode,
        )

    return create_app(settings, opencode_factory=_factory)


@pytest.fixture
def opencode_client(opencode_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(opencode_app, raise_server_exceptions=False) as test_client:
        yield test_client


def _admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


def _mint_token_for_role(app: FastAPI, role_slug: str, *, suffix: str) -> str:
    db = app.state.db
    username = f"{role_slug}-user-{suffix}"
    token = f"{username}-token"
    user = UserRepository(db).create(username)
    role = RoleRepository(db).get_by_slug(role_slug)
    assert role is not None, f"unknown builtin role slug: {role_slug!r}"
    RoleAssignmentRepository(db).assign(SubjectType.USER, user.id, role.id)
    CredentialRepository(db).create_api_token(user.id, token)
    return token


def _sudoed_maintain_headers(
    app: FastAPI, client: TestClient, *, suffix: str
) -> dict[str, str]:
    db = app.state.db
    username = f"maintainer-{suffix}"
    token = f"{username}-token"
    user = UserRepository(db).create(username)
    role = RoleRepository(db).get_by_slug("maintain")
    assert role is not None
    RoleAssignmentRepository(db).assign(SubjectType.USER, user.id, role.id)
    CredentialRepository(db).create_api_token(user.id, token)
    CredentialRepository(db).set_password(user.id, _MAINTAIN_PASSWORD, _CHEAP_ARGON2)
    headers = {"Authorization": f"Bearer {token}"}
    step_up = client.post(
        "/auth/sudo", headers=headers, json={"password": _MAINTAIN_PASSWORD}
    )
    assert step_up.status_code == 200
    return {**headers, "X-Sudo-Token": step_up.json()["token"]}


# --- disabled: no app.state.opencode, every route answers 503 ----------------------


def test_get_status_503_when_opencode_disabled(client: TestClient) -> None:
    response = client.get("/opencode/status")

    assert response.status_code == 503
    assert response.json()["code"] == "ansina.opencode.disabled"


def test_pause_intake_503_when_opencode_disabled(client: TestClient) -> None:
    response = client.post("/opencode/intake/pause")

    assert response.status_code == 503
    assert response.json()["code"] == "ansina.opencode.disabled"


def test_resume_intake_503_when_opencode_disabled(client: TestClient) -> None:
    response = client.post("/opencode/intake/resume")

    assert response.status_code == 503
    assert response.json()["code"] == "ansina.opencode.disabled"


# --- deny-by-default: auth runs before route logic, even the 503 branch ------------


def test_get_status_requires_auth_when_configured(authed_client: TestClient) -> None:
    response = authed_client.get("/opencode/status")

    assert response.status_code == 401


def test_pause_intake_requires_auth_when_configured(
    authed_client: TestClient,
) -> None:
    response = authed_client.post("/opencode/intake/pause")

    assert response.status_code == 401


# --- GET /opencode/status: non-sensitive --------------------------------------------


def test_read_role_can_read_status(
    opencode_app: FastAPI, opencode_client: TestClient
) -> None:
    token = _mint_token_for_role(opencode_app, "read", suffix="1")

    response = opencode_client.get(
        "/opencode/status", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200


def test_get_status_reports_enabled_and_unpaused_by_default(
    opencode_client: TestClient,
) -> None:
    response = opencode_client.get("/opencode/status", headers=_admin_headers())

    assert response.status_code == 200
    body = response.json()
    assert body == {"enabled": True, "intake_paused": False, "active_runs": 0}


def test_get_status_reports_active_runs(opencode_client: TestClient) -> None:
    opencode_client.post(
        "/opencode/runs", headers=_admin_headers(), json={"prompt": "say hello"}
    )

    response = opencode_client.get("/opencode/status", headers=_admin_headers())

    # The fake runner completes near-instantly, so this just pins the field is
    # present and well-typed rather than racing a specific count — the 0/1
    # transition itself is `test_service.py`'s own concern (unit-level, not an
    # HTTP timing assertion).
    assert isinstance(response.json()["active_runs"], int)


# --- pause/resume: sudo-gated (sensitive=True) --------------------------------------


def test_read_role_gets_403_on_pause(
    opencode_app: FastAPI, opencode_client: TestClient
) -> None:
    token = _mint_token_for_role(opencode_app, "read", suffix="2")

    response = opencode_client.post(
        "/opencode/intake/pause", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.forbidden"


def test_write_role_without_sudo_gets_sudo_required_on_pause(
    opencode_app: FastAPI, opencode_client: TestClient
) -> None:
    token = _mint_token_for_role(opencode_app, "write", suffix="3")

    response = opencode_client.post(
        "/opencode/intake/pause", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.auth.sudo_required"


def test_admin_pauses_intake_with_no_sudo_grant_needed(
    opencode_client: TestClient,
) -> None:
    response = opencode_client.post("/opencode/intake/pause", headers=_admin_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["intake_paused"] is True


def test_sudoed_maintain_pauses_intake(
    opencode_app: FastAPI, opencode_client: TestClient
) -> None:
    headers = _sudoed_maintain_headers(opencode_app, opencode_client, suffix="4")

    response = opencode_client.post("/opencode/intake/pause", headers=headers)

    assert response.status_code == 200
    assert response.json()["intake_paused"] is True


def test_paused_intake_refuses_a_submission(opencode_client: TestClient) -> None:
    opencode_client.post("/opencode/intake/pause", headers=_admin_headers())

    response = opencode_client.post(
        "/opencode/runs", headers=_admin_headers(), json={"prompt": "say hello"}
    )

    assert response.status_code == 409
    assert response.json()["code"] == "ansina.opencode.intake_paused"


def test_resume_intake_round_trips_the_flag(opencode_client: TestClient) -> None:
    opencode_client.post("/opencode/intake/pause", headers=_admin_headers())

    response = opencode_client.post("/opencode/intake/resume", headers=_admin_headers())

    assert response.status_code == 200
    assert response.json()["intake_paused"] is False

    submit = opencode_client.post(
        "/opencode/runs", headers=_admin_headers(), json={"prompt": "say hello"}
    )
    assert submit.status_code == 202
