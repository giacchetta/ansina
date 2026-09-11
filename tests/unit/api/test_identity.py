"""Unit tests for `ansina.api.identity` — the shared request-identity helpers. See
issue #30.

`current_principal` only ever reads `request.state.principal` via `getattr`, so a bare
`SimpleNamespace` standing in for `Request` is enough here — no FastAPI app needed;
`tests/unit/api/routes/test_me.py` and `test_sudo.py`/`test_role_assignments.py`
already cover the real-request round trip.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast

from fastapi import Request

from ansina.api.identity import current_principal, no_identity_response
from ansina.api.problems import CODE_UNAUTHORIZED, PROBLEM_MEDIA_TYPE
from ansina.auth.models import User
from ansina.auth.principal import Principal

_USER = User(
    id="user-1",
    username="tester",
    display_name="Tester",
    active=True,
    created_at="2026-01-01T00:00:00Z",
)
_PRINCIPAL = Principal(user=_USER, role_ids=frozenset({"role-1"}))


def _fake_request(*, principal: Principal | None) -> Request:
    state = (
        SimpleNamespace() if principal is None else SimpleNamespace(principal=principal)
    )
    return cast(Request, SimpleNamespace(state=state))


def test_current_principal_returns_the_resolved_principal() -> None:
    request = _fake_request(principal=_PRINCIPAL)

    assert current_principal(request) is _PRINCIPAL


def test_current_principal_returns_none_when_unresolved() -> None:
    request = _fake_request(principal=None)

    assert current_principal(request) is None


def test_no_identity_response_is_a_401_problem_json() -> None:
    response = no_identity_response()

    assert response.status_code == 401
    assert response.media_type == PROBLEM_MEDIA_TYPE
    body = json.loads(bytes(response.body))
    assert body["code"] == CODE_UNAUTHORIZED
