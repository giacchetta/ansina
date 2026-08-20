from __future__ import annotations

from typing import ClassVar

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from ansina.api.problems import problem_response, status_for_error
from ansina.errors import AnsinaError, ConfigurationError


class _TeapotError(AnsinaError):
    code: ClassVar[str] = "ansina.test.teapot"


class _UnmappedError(AnsinaError):
    """No entry in `_STATUS_BY_ERROR_TYPE` of its own — exercises the `AnsinaError`
    fallback in `status_for_error`.
    """

    code: ClassVar[str] = "ansina.test.unmapped"


def test_status_for_error_uses_own_mapping() -> None:
    assert status_for_error(ConfigurationError("bad config")) == 500


def test_status_for_error_falls_back_through_mro() -> None:
    # `_UnmappedError` isn't in the mapping table itself, so this walks up its MRO to
    # `AnsinaError`'s entry rather than the module-level default.
    assert status_for_error(_UnmappedError("oops")) == 500


def test_problem_response_reads_extra_members() -> None:
    response = problem_response(
        status=418,
        code="ansina.test.teapot",
        title="I'm a teapot",
        detail="short and stout",
        extra={"field": "spout"},
    )

    assert response.status_code == 418
    assert response.media_type == "application/problem+json"


class _Body(BaseModel):
    name: str


def _install_test_routes(app: FastAPI) -> None:
    """Routes that exist only to exercise each error door — never mounted outside
    tests.
    """

    @app.get("/__test/ansina-error")
    async def _raise_ansina_error() -> None:
        raise _TeapotError("short and stout", details={"field": "spout"})

    @app.get("/__test/boom")
    async def _raise_unexpected() -> None:
        raise ValueError("something exploded")

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
