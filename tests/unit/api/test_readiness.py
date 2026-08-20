from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ansina.api.readiness import Readiness


def test_no_checks_registered_is_vacuously_ready() -> None:
    readiness = Readiness()

    assert readiness.is_ready is True
    assert readiness.snapshot() == {}


def test_ready_when_every_check_passes() -> None:
    readiness = Readiness()
    readiness.register("a", lambda: True)
    readiness.register("b", lambda: True)

    assert readiness.is_ready is True
    assert readiness.snapshot() == {"a": True, "b": True}


def test_not_ready_when_one_check_fails() -> None:
    readiness = Readiness()
    readiness.register("a", lambda: True)
    readiness.register("b", lambda: False)

    assert readiness.is_ready is False
    assert readiness.snapshot() == {"a": True, "b": False}


def test_checks_are_evaluated_fresh_each_time() -> None:
    readiness = Readiness()
    state = {"ok": False}
    readiness.register("dynamic", lambda: state["ok"])

    assert readiness.is_ready is False
    state["ok"] = True
    assert readiness.is_ready is True


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
