from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ansina import __version__
from ansina.api.readiness import Readiness


def test_healthz_ok_with_no_dependencies(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version_matches_package_version(client: TestClient) -> None:
    response = client.get("/version")

    assert response.status_code == 200
    assert response.json() == {"name": "ansina", "version": __version__}


def test_openapi_schema_is_served_and_lists_all_routes(client: TestClient) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert set(paths) == {
        "/healthz",
        "/readyz",
        "/version",
        "/heart/tick",
        "/heart/tick/pause",
        "/heart/tick/resume",
        "/auth/sudo",
        "/auth/sudo/grants",
        "/auth/users",
        "/auth/users/{user_id}",
        "/auth/users/{user_id}/password",
        "/auth/users/{user_id}/tokens",
        "/auth/users/{user_id}/roles/{role_id}",
        "/auth/groups",
        "/auth/groups/{group_id}",
        "/auth/groups/{group_id}/members/{user_id}",
        "/auth/groups/{group_id}/roles/{role_id}",
        "/auth/roles",
        "/auth/permissions",
    }


def test_readyz_returns_200_when_ready(client: TestClient) -> None:
    response = client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["startup"] is True


def test_readyz_returns_503_problem_json_when_not_ready(
    app: FastAPI, client: TestClient
) -> None:
    readiness: Readiness = app.state.readiness
    readiness.register("dependency", lambda: False)

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "ansina.not_ready"
    assert body["checks"]["dependency"] is False


def test_readyz_reports_the_database_check(client: TestClient) -> None:
    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["checks"]["database"] is True
