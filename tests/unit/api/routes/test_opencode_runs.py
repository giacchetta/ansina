from __future__ import annotations

import asyncio
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
from ansina.opencode.events import RunEvent, RunOutcome, RunRequest
from ansina.opencode.events import RunStatus as OpencodeRunStatus
from ansina.opencode.repository import OpencodeRunRepository
from ansina.opencode.runner import OnEvent
from ansina.opencode.service import OpencodeRunService
from ansina.opencode.telemetry import OpencodeTelemetryWriter
from ansina.storage.database import Database
from ansina.telemetry.rotation import SpooledRotatingWriter

_ADMIN_USERNAME = "opencode-configured-admin"
_ADMIN_TOKEN = "opencode-admin-token-0123456789abcdefgh"
_CHEAP_ARGON2 = Argon2Params(time_cost=1, memory_cost_kib=8, parallelism=1)
_MAINTAIN_PASSWORD = "correct horse battery staple opencode"


class _FakeAgentRunner:
    """A scripted `AgentRunner` — mirrors `tests/unit/opencode/test_service.py`'s
    own fake exactly (duplicated, not imported, for the same cross-file-import
    reason every other fixture in this suite is).
    """

    def __init__(
        self,
        *,
        events: list[RunEvent] | None = None,
        outcome: RunOutcome | None = None,
    ) -> None:
        self.events = events or []
        self.outcome = outcome or RunOutcome(
            status=OpencodeRunStatus.COMPLETED,
            exit_code=0,
            events_emitted=len(self.events),
            events_skipped=0,
            duration_seconds=0.1,
        )

    async def run(self, request: RunRequest, on_event: OnEvent) -> RunOutcome:
        for event in self.events:
            on_event(event)
        return self.outcome


class _HangingAgentRunner:
    """Never returns on its own — mirrors `test_service.py`'s own fake, used here
    to exercise a real in-flight cancellation through the HTTP layer.
    """

    async def run(self, request: RunRequest, on_event: OnEvent) -> RunOutcome:
        await asyncio.Event().wait()
        raise AssertionError("unreachable — only ever cancelled, never finishes")


@pytest.fixture
def fake_runner() -> _FakeAgentRunner:
    return _FakeAgentRunner()


@pytest.fixture
def opencode_app(
    clean_env: None,
    tmp_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_runner: _FakeAgentRunner,
) -> FastAPI:
    """Auth enforced via a configured admin (issue #28) + opencode enabled with a
    fake `AgentRunner` injected through `opencode_factory` — no real `opencode`
    binary, no real preflight. Mirrors `tests/unit/api/routes/test_heart.py`'s own
    `heart_enabled_app` shape.
    """
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
            runner=fake_runner,
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
    """Mint a fresh Maintain user + step it up — the local equivalent of
    `tests/unit/api/conftest.py`'s `sudoed_maintain` fixture, which is bound to
    the global `authed_app`/`authed_client`, not this file's own `opencode_app`.
    """
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


def test_submit_run_503_when_opencode_disabled(client: TestClient) -> None:
    response = client.post("/opencode/runs", json={"prompt": "say hello"})

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "ansina.opencode.disabled"


def test_list_runs_503_when_opencode_disabled(client: TestClient) -> None:
    response = client.get("/opencode/runs")

    assert response.status_code == 503
    assert response.json()["code"] == "ansina.opencode.disabled"


def test_get_run_503_when_opencode_disabled(client: TestClient) -> None:
    response = client.get("/opencode/runs/does-not-exist")

    assert response.status_code == 503
    assert response.json()["code"] == "ansina.opencode.disabled"


def test_cancel_run_503_when_opencode_disabled(client: TestClient) -> None:
    response = client.post("/opencode/runs/does-not-exist/cancel")

    assert response.status_code == 503
    assert response.json()["code"] == "ansina.opencode.disabled"


# --- deny-by-default: auth runs before route logic, even the 503 branch ------------


def test_submit_run_requires_auth_when_configured(authed_client: TestClient) -> None:
    response = authed_client.post("/opencode/runs", json={"prompt": "say hello"})

    assert response.status_code == 401


def test_list_runs_requires_auth_when_configured(authed_client: TestClient) -> None:
    response = authed_client.get("/opencode/runs")

    assert response.status_code == 401


# --- submission: sudo-gated (sensitive=True) ----------------------------------------


def test_read_role_gets_403_on_submit(
    opencode_app: FastAPI, opencode_client: TestClient
) -> None:
    token = _mint_token_for_role(opencode_app, "read", suffix="1")

    response = opencode_client.post(
        "/opencode/runs",
        headers={"Authorization": f"Bearer {token}"},
        json={"prompt": "say hello"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.forbidden"


def test_write_role_without_sudo_gets_sudo_required_on_submit(
    opencode_app: FastAPI, opencode_client: TestClient
) -> None:
    token = _mint_token_for_role(opencode_app, "write", suffix="2")

    response = opencode_client.post(
        "/opencode/runs",
        headers={"Authorization": f"Bearer {token}"},
        json={"prompt": "say hello"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.auth.sudo_required"


def test_admin_submits_a_run_with_no_sudo_grant_needed(
    opencode_client: TestClient,
) -> None:
    response = opencode_client.post(
        "/opencode/runs", headers=_admin_headers(), json={"prompt": "say hello"}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["prompt"] == "say hello"


def test_sudoed_maintain_submits_a_run(
    opencode_app: FastAPI, opencode_client: TestClient
) -> None:
    headers = _sudoed_maintain_headers(opencode_app, opencode_client, suffix="3")

    response = opencode_client.post(
        "/opencode/runs", headers=headers, json={"prompt": "say hello"}
    )

    assert response.status_code == 202


def test_submit_run_refuses_an_oversized_prompt(
    opencode_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Rebuild with a tiny `max_prompt_chars` — the settings-driven refusal path.
    monkeypatch.setenv("ANSINA_OPENCODE__MAX_PROMPT_CHARS", "3")
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

    app = create_app(settings, opencode_factory=_factory)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/opencode/runs", headers=_admin_headers(), json={"prompt": "way too long"}
        )

    assert response.status_code == 400
    assert response.json()["code"] == "ansina.request.invalid"


def test_submit_run_rejects_a_malformed_combination(
    opencode_client: TestClient,
) -> None:
    response = opencode_client.post(
        "/opencode/runs",
        headers=_admin_headers(),
        json={"prompt": "x", "session_id": "s", "continue_last": True},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "ansina.request.invalid"


# --- listing/paging: GET is non-sensitive -------------------------------------------


def test_read_role_can_list_runs(
    opencode_app: FastAPI, opencode_client: TestClient
) -> None:
    token = _mint_token_for_role(opencode_app, "read", suffix="4")

    response = opencode_client.get(
        "/opencode/runs", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200


def test_list_runs_on_an_empty_table(opencode_client: TestClient) -> None:
    response = opencode_client.get("/opencode/runs", headers=_admin_headers())

    assert response.status_code == 200
    body = response.json()
    assert body == {"runs": [], "count": 0, "limit": 50, "has_more": False}


def test_list_runs_returns_newest_first(opencode_client: TestClient) -> None:
    opencode_client.post(
        "/opencode/runs", headers=_admin_headers(), json={"prompt": "first"}
    )
    opencode_client.post(
        "/opencode/runs", headers=_admin_headers(), json={"prompt": "second"}
    )

    response = opencode_client.get("/opencode/runs", headers=_admin_headers())

    body = response.json()
    assert [r["prompt"] for r in body["runs"]] == ["second", "first"]


def test_list_runs_respects_limit(opencode_client: TestClient) -> None:
    for i in range(3):
        opencode_client.post(
            "/opencode/runs", headers=_admin_headers(), json={"prompt": str(i)}
        )

    response = opencode_client.get(
        "/opencode/runs", headers=_admin_headers(), params={"limit": 1}
    )

    body = response.json()
    assert body["count"] == 1
    assert body["has_more"] is True


def test_list_runs_rejects_a_malformed_since(opencode_client: TestClient) -> None:
    response = opencode_client.get(
        "/opencode/runs",
        headers=_admin_headers(),
        params={"since": "not-a-timestamp"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "ansina.request.invalid"


def test_list_runs_rejects_limit_over_the_ceiling(opencode_client: TestClient) -> None:
    response = opencode_client.get(
        "/opencode/runs", headers=_admin_headers(), params={"limit": 10_000}
    )

    assert response.status_code == 422


# --- GET one run ---------------------------------------------------------------------


def test_get_run_returns_404_for_an_unknown_id(opencode_client: TestClient) -> None:
    response = opencode_client.get(
        "/opencode/runs/does-not-exist", headers=_admin_headers()
    )

    assert response.status_code == 404
    assert response.json()["code"] == "ansina.not_found"


def test_get_run_returns_the_submitted_run(opencode_client: TestClient) -> None:
    submitted = opencode_client.post(
        "/opencode/runs", headers=_admin_headers(), json={"prompt": "say hello"}
    ).json()

    response = opencode_client.get(
        f"/opencode/runs/{submitted['id']}", headers=_admin_headers()
    )

    assert response.status_code == 200
    assert response.json()["id"] == submitted["id"]


def test_get_run_requires_auth_when_configured(authed_client: TestClient) -> None:
    response = authed_client.get("/opencode/runs/some-id")

    assert response.status_code == 401


# --- cancellation: sudo-gated (sensitive=True) --------------------------------------


def test_cancel_run_returns_404_for_an_unknown_id(opencode_client: TestClient) -> None:
    response = opencode_client.post(
        "/opencode/runs/does-not-exist/cancel", headers=_admin_headers()
    )

    assert response.status_code == 404


def test_cancel_run_returns_404_for_an_already_terminal_run(
    opencode_client: TestClient,
) -> None:
    submitted = opencode_client.post(
        "/opencode/runs", headers=_admin_headers(), json={"prompt": "say hello"}
    ).json()

    # The fake runner completes near-instantly with no real await, but the
    # background task still needs a few event-loop turns to actually run and
    # record the terminal status — poll rather than assume one request's worth
    # of slack is always enough.
    for _ in range(50):
        status = opencode_client.get(
            f"/opencode/runs/{submitted['id']}", headers=_admin_headers()
        ).json()["status"]
        if status != "queued" and status != "running":
            break
    else:
        pytest.fail("run never reached a terminal status")

    response = opencode_client.post(
        f"/opencode/runs/{submitted['id']}/cancel", headers=_admin_headers()
    )

    assert response.status_code == 404


def test_read_role_gets_403_on_cancel(
    opencode_app: FastAPI, opencode_client: TestClient
) -> None:
    token = _mint_token_for_role(opencode_app, "read", suffix="5")

    response = opencode_client.post(
        "/opencode/runs/some-id/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403


def test_cancel_an_inflight_run_succeeds(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            runner=_HangingAgentRunner(),
            repository=OpencodeRunRepository(db),
            telemetry=telemetry,
            settings=settings.opencode,
        )

    monkeypatch.setenv("ANSINA_SECURITY__ADMIN_USERNAME", _ADMIN_USERNAME)
    monkeypatch.setenv("ANSINA_SECURITY__API_TOKEN", _ADMIN_TOKEN)
    monkeypatch.setenv("ANSINA_OPENCODE__ENABLED", "true")
    monkeypatch.setenv("ANSINA_OPENCODE__WORKSPACE_DIR", str(tmp_cwd / "workspace"))
    monkeypatch.setenv("ANSINA_TELEMETRY__ENABLED", "true")
    monkeypatch.setenv("ANSINA_TELEMETRY__SPOOL_DIR", str(tmp_cwd / "spool"))
    settings = load_settings()
    app = create_app(settings, opencode_factory=_factory)

    with TestClient(app, raise_server_exceptions=False) as client:
        submitted = client.post(
            "/opencode/runs", headers=_admin_headers(), json={"prompt": "say hello"}
        ).json()

        response = client.post(
            f"/opencode/runs/{submitted['id']}/cancel", headers=_admin_headers()
        )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
