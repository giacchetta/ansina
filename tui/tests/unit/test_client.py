from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ansina_tui.client import ApiClient, HostUnreachableError


def _capturing_transport(
    captured: list[httpx.Request], response: httpx.Response
) -> httpx.MockTransport:
    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return response

    return httpx.MockTransport(_handler)


def test_bearer_header_attached_when_token_given(
    json_response: Callable[..., httpx.Response],
) -> None:
    captured: list[httpx.Request] = []
    transport = _capturing_transport(captured, json_response(200, {"ok": True}))
    client = ApiClient("http://x", token="my-token", transport=transport)

    client.get("/healthz")

    assert captured[0].headers["authorization"] == "Bearer my-token"


def test_bearer_header_omitted_when_no_token(
    json_response: Callable[..., httpx.Response],
) -> None:
    captured: list[httpx.Request] = []
    transport = _capturing_transport(captured, json_response(200, {"ok": True}))
    client = ApiClient("http://x", transport=transport)

    client.get("/healthz")

    assert "authorization" not in captured[0].headers


def test_sudo_token_attached_while_unexpired(
    json_response: Callable[..., httpx.Response],
    clock: Callable[[], datetime],
    iso: Callable[[timedelta], str],
) -> None:
    captured: list[httpx.Request] = []
    transport = _capturing_transport(captured, json_response(200, {"ok": True}))
    client = ApiClient(
        "http://x",
        sudo_token="sudo-abc",
        sudo_expires_at=iso(timedelta(minutes=5)),
        now=clock,
        transport=transport,
    )

    client.get("/healthz")

    assert captured[0].headers["x-sudo-token"] == "sudo-abc"


def test_sudo_token_omitted_once_expired(
    json_response: Callable[..., httpx.Response],
    clock: Callable[[], datetime],
    iso: Callable[[timedelta], str],
) -> None:
    captured: list[httpx.Request] = []
    transport = _capturing_transport(captured, json_response(200, {"ok": True}))
    client = ApiClient(
        "http://x",
        sudo_token="sudo-abc",
        sudo_expires_at=iso(-timedelta(minutes=5)),
        now=clock,
        transport=transport,
    )

    client.get("/healthz")

    assert "x-sudo-token" not in captured[0].headers


def test_sudo_token_attached_using_the_real_default_clock_when_none_injected(
    json_response: Callable[..., httpx.Response],
) -> None:
    """No `now=` passed — exercises `_default_now` (real wall-clock time) rather than
    a test's frozen `clock` fixture."""
    captured: list[httpx.Request] = []
    transport = _capturing_transport(captured, json_response(200, {"ok": True}))
    far_future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    client = ApiClient(
        "http://x",
        sudo_token="sudo-abc",
        sudo_expires_at=far_future,
        transport=transport,
    )

    client.get("/healthz")

    assert captured[0].headers["x-sudo-token"] == "sudo-abc"


def test_sudo_token_omitted_when_expiry_is_malformed(
    json_response: Callable[..., httpx.Response],
) -> None:
    captured: list[httpx.Request] = []
    transport = _capturing_transport(captured, json_response(200, {"ok": True}))
    client = ApiClient(
        "http://x",
        sudo_token="sudo-abc",
        sudo_expires_at="not-a-date",
        transport=transport,
    )

    client.get("/healthz")

    assert "x-sudo-token" not in captured[0].headers


def test_request_id_captured_from_response_header(
    json_response: Callable[..., httpx.Response],
) -> None:
    response = json_response(200, {"ok": True}, headers={"X-Request-Id": "req-42"})
    transport = _capturing_transport([], response)
    client = ApiClient("http://x", transport=transport)

    result = client.get("/healthz")

    assert result.request_id == "req-42"
    assert result.ok is True


def test_request_id_none_when_absent(
    json_response: Callable[..., httpx.Response],
) -> None:
    transport = _capturing_transport([], json_response(200, {"ok": True}))
    client = ApiClient("http://x", transport=transport)

    result = client.get("/healthz")

    assert result.request_id is None


def test_transport_error_becomes_host_unreachable_error(
    unreachable_transport: httpx.MockTransport,
) -> None:
    client = ApiClient("http://x", transport=unreachable_transport)

    with pytest.raises(HostUnreachableError) as exc_info:
        client.get("/healthz")

    assert "http://x" in str(exc_info.value)


def test_empty_body_has_no_json(json_response: Callable[..., httpx.Response]) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(204))
    client = ApiClient("http://x", transport=transport)

    result = client.get("/healthz")

    assert result.json_body is None
    assert result.problem is None


def test_non_json_body_degrades_to_none_json_body() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    client = ApiClient("http://x", transport=httpx.MockTransport(_handler))

    result = client.get("/healthz")

    assert result.json_body is None


def test_client_is_a_context_manager(
    json_response: Callable[..., httpx.Response],
) -> None:
    transport = httpx.MockTransport(lambda request: json_response(200, {"ok": True}))
    with ApiClient("http://x", transport=transport) as client:
        result = client.get("/healthz")
    assert result.ok


def test_request_supports_json_body_and_params(
    json_response: Callable[..., httpx.Response],
) -> None:
    captured: list[httpx.Request] = []
    transport = _capturing_transport(captured, json_response(200, {"ok": True}))
    client = ApiClient("http://x", transport=transport)

    client.request("POST", "/thing", json_body={"a": 1}, params={"b": "2"})

    assert captured[0].method == "POST"
    assert captured[0].url.params["b"] == "2"
