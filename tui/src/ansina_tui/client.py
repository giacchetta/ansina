"""A thin `httpx.Client` wrapper: base URL, bearer auth, sudo elevation, and the
`X-Request-Id` correlation id every daemon response carries (`ansina.api.middleware`).

The `transport=` constructor parameter is how every test drives this without touching
a real network — an `httpx.MockTransport` stands in for the daemon.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from ansina_tui.problems import ApiProblem, parse_problem

_REQUEST_ID_HEADER = "x-request-id"

# Headers `ApiClient` attaches itself and a caller-supplied `-H` (`commands/api.py`) is
# never allowed to silently replace — checked case-insensitively against a caller's
# header names. Exported so `commands/api.py` can warn (not just silently override).
RESERVED_HEADERS = frozenset({"authorization", "x-sudo-token"})


class HostUnreachableError(Exception):
    """The daemon could not be reached at all — connect/timeout/DNS failure."""

    def __init__(self, host: str, cause: Exception) -> None:
        self.host = host
        self.cause = cause
        super().__init__(f"Could not reach {host}: {cause}")


@dataclass(frozen=True, slots=True)
class ApiResponse:
    """One HTTP round-trip, with the daemon's `problem+json` body (if any) already
    parsed and its correlation id lifted out for easy display."""

    status_code: int
    request_id: str | None
    json_body: Any
    problem: ApiProblem | None
    text: str
    headers: Mapping[str, str]

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


def _default_now() -> datetime:
    return datetime.now(UTC)


def parse_expiry(raw: str | None) -> datetime | None:
    """`sudo_expires_at` as stored in `hosts.toml`. A missing or malformed value is
    treated as "no live grant" — the safe default is to not attach `X-Sudo-Token`,
    never to attach one whose freshness we couldn't verify. Exported (not `_`-private)
    since `ansina_tui.session` (issue #32) reuses this exact parse to report sudo
    state without duplicating it."""
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class ApiClient:
    """Talks to exactly one Ansina daemon host."""

    def __init__(
        self,
        host: str,
        *,
        token: str | None = None,
        sudo_token: str | None = None,
        sudo_expires_at: str | None = None,
        now: Callable[[], datetime] = _default_now,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._host = host
        self._token = token
        self._sudo_token = sudo_token
        self._sudo_expiry = parse_expiry(sudo_expires_at)
        self._now = now
        self._client = httpx.Client(base_url=host, transport=transport, timeout=timeout)

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get(self, path: str) -> ApiResponse:
        return self.request("GET", path)

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,  # noqa: ANN401 — an arbitrary JSON-serializable request body
        content: str | bytes | None = None,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> ApiResponse:
        """`content=` sends a raw body (`commands/api.py`'s `--input`) instead of
        `json_body=`'s JSON-encoded one — httpx's own `encode_request` checks `content`
        before `json`, so the two never conflict.

        `headers=` merges a caller's own headers (`commands/api.py`'s `-H`) — applied
        *first*, then `Authorization`/`X-Sudo-Token` are set by plain assignment, never
        `setdefault`, so a caller-supplied header can never silently replace either.
        """
        request_headers: dict[str, str] = dict(headers) if headers else {}
        if self._token:
            request_headers["Authorization"] = f"Bearer {self._token}"
        if self._sudo_token and self._sudo_expiry and self._sudo_expiry > self._now():
            request_headers["X-Sudo-Token"] = self._sudo_token

        try:
            response = self._client.request(
                method,
                path,
                headers=request_headers,
                json=json_body,
                content=content,
                params=params,
            )
        except httpx.TransportError as exc:
            raise HostUnreachableError(self._host, exc) from exc

        try:
            body: Any = response.json() if response.content else None
        except ValueError:
            body = None

        return ApiResponse(
            status_code=response.status_code,
            request_id=response.headers.get(_REQUEST_ID_HEADER),
            json_body=body,
            problem=parse_problem(body),
            text=response.text,
            headers=response.headers,
        )
