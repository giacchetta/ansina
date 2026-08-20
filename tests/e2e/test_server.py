"""Black-box: launches `python -m ansina` as a real subprocess and talks HTTP only.

Never imports `ansina.api` internals (blueprint §5) — this is the gate that answers
"does a fresh build actually work," independent of whether the unit tests pass.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
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


def _launch_server(
    tmp_path: Path, *, env: dict[str, str] | None = None
) -> Iterator[str]:
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

        yield base_url
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
    yield from _launch_server(tmp_path)


@pytest.fixture
def authed_server(tmp_path: Path) -> Iterator[str]:
    """ANSINA_SECURITY__API_TOKEN set in the child process — auth enforced."""
    yield from _launch_server(tmp_path, env={"ANSINA_SECURITY__API_TOKEN": _E2E_TOKEN})


def test_healthz(server: str) -> None:
    response = httpx.get(f"{server}/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz(server: str) -> None:
    response = httpx.get(f"{server}/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


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
