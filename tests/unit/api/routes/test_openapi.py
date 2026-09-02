from __future__ import annotations

from fastapi.testclient import TestClient


def test_openapi_json_is_served(client: TestClient) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert "/version" in response.json()["paths"]


def test_openapi_route_itself_is_excluded_from_its_own_schema(
    client: TestClient,
) -> None:
    response = client.get("/openapi.json")

    assert "/openapi.json" not in response.json()["paths"]


def test_docs_and_redoc_are_gone(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/docs/oauth2-redirect").status_code == 404


def test_openapi_json_requires_a_token_when_auth_is_enabled(
    authed_client: TestClient,
) -> None:
    response = authed_client.get("/openapi.json")

    assert response.status_code == 401


def test_openapi_json_accepts_a_valid_token(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.get(
        "/openapi.json", headers={"Authorization": f"Bearer {authed_token}"}
    )

    assert response.status_code == 200
