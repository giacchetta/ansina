"""Black-box: launches `python -m ansina` as a real subprocess and talks HTTP only.

Never imports `ansina.api` internals (blueprint §5) — this is the gate that answers
"does a fresh build actually work," independent of whether the unit tests pass.
"""

from __future__ import annotations

import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

_STARTUP_TIMEOUT_S = 15.0
_POLL_INTERVAL_S = 0.1

# Long enough to clear `SecuritySettings.api_token`'s `min_length=16`.
_E2E_TOKEN = "e2e-test-token-0123456789"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@dataclass
class Server:
    """A launched `python -m ansina` subprocess, still healthy when yielded.

    Carries the raw `Popen` (not just the base URL) so a test can inspect the exit
    code and captured output after shutdown — e.g. to prove a clean SIGTERM shutdown,
    not just that the process eventually dies.
    """

    base_url: str
    process: subprocess.Popen[str]


@contextmanager
def _launch_server(
    tmp_path: Path, *, env: dict[str, str] | None = None
) -> Iterator[Server]:
    """Launch `python -m ansina` against `tmp_path`'s config, yielding its `Server`.

    A `@contextmanager` rather than a bare generator so a test can start, stop, and
    restart the server against the *same* `tmp_path` — e.g. to prove a migration
    applied on the first boot is not re-applied on the second.
    """
    port = _free_port()
    (tmp_path / "ansina.toml").write_text(
        f'[server]\nhost = "127.0.0.1"\nport = {port}\n'
        f'[database]\npath = "{(tmp_path / "ansina.db").as_posix()}"\n',
        encoding="utf-8",
    )
    base_url = f"http://127.0.0.1:{port}"

    process = subprocess.Popen(  # fixed argv, no shell, no untrusted input
        [sys.executable, "-m", "ansina"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, **(env or {})},
    )
    try:
        deadline = time.monotonic() + _STARTUP_TIMEOUT_S
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                pytest.fail(
                    f"ansina exited early (code {process.returncode}):\n{output}"
                )
            try:
                response = httpx.get(f"{base_url}/healthz", timeout=1.0)
                if response.status_code == 200:
                    break
            except httpx.HTTPError as exc:
                last_error = exc
            time.sleep(_POLL_INTERVAL_S)
        else:
            process.kill()
            raise TimeoutError(f"ansina never became healthy: {last_error}")

        yield Server(base_url=base_url, process=process)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    """No ANSINA_SECURITY__API_TOKEN — auth disabled, every route reachable."""
    with _launch_server(tmp_path) as srv:
        yield srv.base_url


@pytest.fixture
def authed_server(tmp_path: Path) -> Iterator[str]:
    """ANSINA_SECURITY__API_TOKEN set in the child process — auth enforced."""
    with _launch_server(
        tmp_path, env={"ANSINA_SECURITY__API_TOKEN": _E2E_TOKEN}
    ) as srv:
        yield srv.base_url


def test_healthz(server: str) -> None:
    response = httpx.get(f"{server}/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz(server: str) -> None:
    response = httpx.get(f"{server}/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] is True


def test_version(server: str) -> None:
    response = httpx.get(f"{server}/version")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "ansina"
    assert body["version"]


def test_openapi_schema(server: str) -> None:
    response = httpx.get(f"{server}/openapi.json")

    assert response.status_code == 200
    assert set(response.json()["paths"]) == {"/healthz", "/readyz", "/version"}


def test_request_id_is_echoed(server: str) -> None:
    response = httpx.get(f"{server}/healthz", headers={"X-Request-ID": "e2e-trace"})

    assert response.headers["x-request-id"] == "e2e-trace"


def test_unknown_path_is_problem_json(server: str) -> None:
    response = httpx.get(f"{server}/nope")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "ansina.not_found"


def test_authed_healthz_reachable_without_token(authed_server: str) -> None:
    response = httpx.get(f"{authed_server}/healthz")

    assert response.status_code == 200


def test_authed_protected_route_rejects_missing_token(authed_server: str) -> None:
    response = httpx.get(f"{authed_server}/version")

    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "ansina.unauthorized"


def test_authed_protected_route_accepts_valid_token(authed_server: str) -> None:
    response = httpx.get(
        f"{authed_server}/version",
        headers={"Authorization": f"Bearer {_E2E_TOKEN}"},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "ansina"


def test_migration_survives_a_restart(tmp_path: Path) -> None:
    """A real, black-box run of issue #6's acceptance criteria: on first boot the
    database file is created and migrated, and a second boot against the same file
    does not re-apply migration 0001 — checked from outside the process, via a plain
    `sqlite3` connection to the file the server wrote.
    """
    with _launch_server(tmp_path) as srv:
        response = httpx.get(f"{srv.base_url}/readyz")
        assert response.json()["checks"]["database"] is True

    db_path = tmp_path / "ansina.db"
    assert db_path.exists()
    with sqlite3.connect(db_path) as conn:
        (journal_mode,) = conn.execute("PRAGMA journal_mode").fetchone()
        assert journal_mode.lower() == "wal"
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
        assert rows == [(1,)]

    # Boot again against the same tmp_path (same ansina.toml, same db file).
    with _launch_server(tmp_path) as srv:
        response = httpx.get(f"{srv.base_url}/readyz")
        assert response.json()["checks"]["database"] is True

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
        assert rows == [(1,)]  # still exactly one row — 0001 was not re-applied


def test_shuts_down_cleanly(tmp_path: Path) -> None:
    """Issue #16's M0 E2E coverage list includes "the process shuts down cleanly" —
    unlike every other test here, this one inspects the subprocess itself (exit code,
    captured output) rather than just its HTTP responses, since a stuck or crashing
    shutdown wouldn't show up in any response the server sent while still running.
    """
    with _launch_server(tmp_path) as srv:
        response = httpx.get(f"{srv.base_url}/healthz")
        assert response.status_code == 200
        process = srv.process

    # By the time `_launch_server`'s `finally` block returns control here, it has
    # already sent SIGTERM (`Popen.terminate()`) and waited for exit.
    assert process.returncode == -signal.SIGTERM
    output = process.stdout.read() if process.stdout else ""
    assert "Traceback" not in output
