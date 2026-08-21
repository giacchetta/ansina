from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from ansina.api.middleware import _extract_inbound_id


def test_extract_inbound_id_absent() -> None:
    assert _extract_inbound_id({"headers": []}) is None


def test_extract_inbound_id_present() -> None:
    scope = {"headers": [(b"x-request-id", b"abc123")]}
    assert _extract_inbound_id(scope) == "abc123"


def test_extract_inbound_id_rejects_undecodable_bytes() -> None:
    # A byte sequence that isn't valid ASCII — the `UnicodeDecodeError` branch.
    scope = {"headers": [(b"x-request-id", b"\xff\xfe")]}
    assert _extract_inbound_id(scope) is None


def test_extract_inbound_id_rejects_empty_value() -> None:
    scope = {"headers": [(b"x-request-id", b"")]}
    assert _extract_inbound_id(scope) is None


def test_request_id_is_minted_when_absent(client: TestClient) -> None:
    response = client.get("/healthz")

    request_id = response.headers.get("x-request-id")
    assert request_id
    assert len(request_id) > 0


def test_inbound_request_id_is_honored(client: TestClient) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": "caller-supplied-id"})

    assert response.headers["x-request-id"] == "caller-supplied-id"


def test_oversized_inbound_id_is_replaced(client: TestClient) -> None:
    oversized = "x" * 200
    response = client.get("/healthz", headers={"X-Request-ID": oversized})

    assert response.headers["x-request-id"] != oversized
    assert len(response.headers["x-request-id"]) <= 128


def test_non_printable_inbound_id_is_replaced(client: TestClient) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": "bad\tid"})

    assert response.headers["x-request-id"] != "bad\tid"


def test_request_id_appears_in_emitted_log_lines(
    client: TestClient,
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": "trace-me"})

    assert response.headers["x-request-id"] == "trace-me"
    logs = captured_logs()
    assert any(entry.get("request_id") == "trace-me" for entry in logs)
