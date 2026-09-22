from __future__ import annotations

from typing import ClassVar

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from ansina.auth.login_throttle import LoginThrottledError
from ansina.errors import AnsinaError


class _TeapotError(AnsinaError):
    code: ClassVar[str] = "ansina.test.teapot"


class _Body(BaseModel):
    name: str


def _install_test_routes(app: FastAPI) -> None:
    """Routes that exist only to exercise each error door — never mounted outside
    tests.
    """

    @app.get("/__test/ansina-error")
    async def _raise_ansina_error() -> None:
        raise _TeapotError("short and stout", details={"field": "spout"})

    @app.get("/__test/rate-limited")
    async def _raise_rate_limited() -> None:
        raise _TeapotError("slow down", details={"retry_after_seconds": 42.7})

    @app.get("/__test/boom")
    async def _raise_unexpected() -> None:
        raise ValueError("something exploded")

    @app.get("/__test/login-throttled")
    async def _raise_login_throttled() -> None:
        # Issue #49: `LoginThrottle`'s own error, with no HTTP caller yet (that's
        # #50's job) — raised directly here to prove it needs no handler-side special
        # case of its own, riding the same generic `retry_after_seconds` -> header
        # path `_TeapotError` above already proves.
        raise LoginThrottledError(
            "too many failed login attempts",
            details={"retry_after_seconds": 60.0},
        )

    @app.post("/__test/validated")
    async def _validated(body: _Body) -> _Body:
        return body


def test_ansina_error_becomes_problem_json_with_stable_code(app: FastAPI) -> None:
    _install_test_routes(app)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/__test/ansina-error")

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "ansina.test.teapot"
    assert body["detail"] == "short and stout"
    assert body["field"] == "spout"
    assert "request_id" in body


def test_retry_after_seconds_detail_becomes_a_real_header(app: FastAPI) -> None:
    """Issue #26: `SudoLockedOutError` is the first `AnsinaError` shaped as a rate
    limit — any `AnsinaError` carrying `details["retry_after_seconds"]` gets a real
    `Retry-After` header alongside the body's own copy, for a client that honors the
    header without parsing JSON.
    """
    _install_test_routes(app)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/__test/rate-limited")

    assert response.headers["retry-after"] == "42"
    assert response.json()["retry_after_seconds"] == 42.7


def test_login_throttled_error_is_429_with_retry_after(app: FastAPI) -> None:
    """Issue #49: `LoginThrottledError` maps to 429 (`api.problems`) and rides the
    same generic `retry_after_seconds` -> `Retry-After` header path issue #26
    introduced — no handler-side special case needed.
    """
    _install_test_routes(app)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/__test/login-throttled")

    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    body = response.json()
    assert body["code"] == "ansina.auth.login_throttled"
    assert body["retry_after_seconds"] == 60.0


def test_unknown_path_is_404_problem_json(client: TestClient) -> None:
    response = client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "ansina.not_found"


def test_wrong_method_is_405_problem_json(client: TestClient) -> None:
    response = client.post("/healthz")

    assert response.status_code == 405
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "ansina.method_not_allowed"


def test_bad_body_is_422_with_errors(app: FastAPI) -> None:
    _install_test_routes(app)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/__test/validated", json={})

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "ansina.request.invalid"
    assert body["errors"]
    assert "input" not in body["errors"][0]


def test_unhandled_exception_is_500_with_no_internals_leaked(app: FastAPI) -> None:
    _install_test_routes(app)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/__test/boom")

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "ansina.internal_error"
    assert "something exploded" not in body["detail"]
