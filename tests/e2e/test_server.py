"""Black-box: launches `python -m ansina` as a real subprocess and talks HTTP only.

Never imports `ansina.api` internals (blueprint §5) — this is the gate that answers
"does a fresh build actually work," independent of whether the unit tests pass.
"""

from __future__ import annotations

import base64
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

# The one exception to this module's "never import `ansina.api` internals" rule
# (blueprint §5): `ansina.auth.hashing` is the domain-layer credential-hashing
# scheme, not an API-layer internal — reusing it here to seed a token directly into
# the running server's SQLite file means this test never re-implements salted-SHA256
# by hand, and fails loudly (an import error, not a silently-wrong hash) if that
# scheme ever changes shape.
from ansina.auth.hashing import Argon2Params, hash_password, hash_token, new_token_salt

_STARTUP_TIMEOUT_S = 15.0
_POLL_INTERVAL_S = 0.1

# Long enough and high-entropy enough to clear `SecuritySettings.api_token`'s
# strength bar (>=32 chars, base64url charset, >=2.5 bits/char) — see
# `config/settings.py`'s `_TOKEN_MIN_LENGTH`/`_TOKEN_CHARSET`/
# `_TOKEN_MIN_ENTROPY_BITS_PER_CHAR`. As of issue #28 this is the *configured admin's*
# credential (`ANSINA_SECURITY__API_TOKEN`, paired with `ADMIN_USERNAME` below) — an
# ordinary Admin user, unrelated to the bootstrap identity's own auto-generated one.
_E2E_ADMIN_USERNAME = "e2e-configured-admin"
_E2E_TOKEN = "e2e-test-token-0123456789abcdefghij"
_E2E_READ_TOKEN = "e2e-read-role-token-0123456789abcd"
_E2E_MAINTAIN_TOKEN = "e2e-maintain-role-token-0123456789ab"
_E2E_MAINTAIN_PASSWORD = "correct horse battery staple e2e"
# Cheap, test-only argon2id work factors (matches `tests/unit/auth/conftest.py`'s
# `cheap_argon2` fixture) — the running server's own configured params never matter
# for *verifying* this hash: argon2-cffi parses them back out of the PHC-format
# string itself (see `ansina.auth.hashing`'s module docstring).
_E2E_CHEAP_ARGON2 = Argon2Params(time_cost=1, memory_cost_kib=8, parallelism=1)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


_MOCK_IDP_KID = "e2e-mock-idp-key"


class _MockIdpHandler(BaseHTTPRequestHandler):
    """Serves the three endpoints issue #43's login exchange calls: OIDC discovery,
    JWKS, and the token endpoint. `self.server.idp` is the owning `MockIdp` (below,
    set by `_mock_idp()`) — this handler only ever reads its already-computed
    documents and its mutable `next_claims`.
    """

    def log_message(self, format: str, *args: object) -> None:
        pass  # keep pytest's captured output free of the stdlib server's own logging

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        idp: MockIdp = self.server.idp  # type: ignore[attr-defined]
        path = urlparse(self.path).path
        if path == "/.well-known/openid-configuration":
            self._send_json(200, idp.discovery_document())
        elif path == "/jwks":
            self._send_json(200, idp.jwks_document())
        else:
            self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        idp: MockIdp = self.server.idp  # type: ignore[attr-defined]
        path = urlparse(self.path).path
        if path != "/token":
            self._send_json(404, {"error": "not_found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)  # form body itself is unused — real code is opaque here
        id_token = idp.sign_id_token()
        self._send_json(
            200,
            {"id_token": id_token, "access_token": "unused", "token_type": "Bearer"},
        )


@dataclass
class MockIdp:
    """A real (if minimal) OIDC IdP on loopback — the black-box counterpart to
    `tests/conftest.py`'s `FakeOidcHttpClient`: `python -m ansina` (a real subprocess)
    talks to this over a real socket, so this is the thing that actually proves
    issue #43's login exchange works end to end, not just that its pieces are wired
    correctly in isolation.

    `next_claims` is mutated by the test between logins to change what the *next*
    `/token` response's `id_token` asserts — the seam AC #4's "claims change between
    two logins for the same subject" scenario drives directly.
    """

    port: int
    key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
    )
    next_claims: dict[str, Any] = field(default_factory=dict)

    @property
    def issuer(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def discovery_document(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": f"{self.issuer}/jwks",
        }

    def jwks_document(self) -> dict[str, Any]:
        numbers = self.key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "kid": _MOCK_IDP_KID,
                    "alg": "RS256",
                    "n": _b64url_uint(numbers.n),
                    "e": _b64url_uint(numbers.e),
                }
            ]
        }

    def sign_id_token(self) -> str:
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": self.next_claims.get("aud", "e2e-oidc-client"),
            "sub": self.next_claims.get("sub", "e2e-subject"),
            "exp": now + 300,
            "iat": now,
            **self.next_claims,
        }
        return jwt.encode(
            claims, self.key, algorithm="RS256", headers={"kid": _MOCK_IDP_KID}
        )

    def set_next_login(
        self, *, sub: str, nonce: str, extra_claims: dict[str, Any] | None = None
    ) -> None:
        """Configure what the *next* `/token` call's `id_token` will assert —
        `nonce` must be the value ansina's own `POST /auth/oidc/login` response
        embedded in `authorization_url`, since `GET /auth/oidc/callback` validates
        it against what it stored server-side.
        """
        self.next_claims = {
            "aud": "e2e-oidc-client",
            "sub": sub,
            "nonce": nonce,
            **(extra_claims or {}),
        }


@contextmanager
def _mock_idp() -> Iterator[MockIdp]:
    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _MockIdpHandler)
    idp = MockIdp(port=port)
    httpd.idp = idp  # type: ignore[attr-defined]  # handlers read `self.server` back as `idp`
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield idp
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def _nonce_from_authorization_url(authorization_url: str) -> str:
    return parse_qs(urlparse(authorization_url).query)["nonce"][0]


@dataclass
class Server:
    """A launched `python -m ansina` subprocess, still healthy when yielded.

    Carries the raw `Popen` (not just the base URL) so a test can inspect the exit
    code and captured output after shutdown — e.g. to prove a clean SIGTERM shutdown,
    not just that the process eventually dies.
    """

    base_url: str
    process: subprocess.Popen[str]


@contextmanager
def _launch_server(
    tmp_path: Path, *, env: dict[str, str] | None = None
) -> Iterator[Server]:
    """Launch `python -m ansina` against `tmp_path`'s config, yielding its `Server`.

    A `@contextmanager` rather than a bare generator so a test can start, stop, and
    restart the server against the *same* `tmp_path` — e.g. to prove a migration
    applied on the first boot is not re-applied on the second.
    """
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

        yield Server(base_url=base_url, process=process)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    """`ANSINA_SECURITY__ENABLED=false` — auth disabled, every route reachable.

    Since issue #24, an *unset* `ANSINA_SECURITY__API_TOKEN` no longer implies "no
    auth" — `security.enabled` defaults to `true` and Ansina would instead
    auto-generate and enforce its own bootstrap token — so dev mode has to be
    requested explicitly here.
    """
    with _launch_server(tmp_path, env={"ANSINA_SECURITY__ENABLED": "false"}) as srv:
        yield srv.base_url


@pytest.fixture
def authed_server(tmp_path: Path) -> Iterator[str]:
    """`ANSINA_SECURITY__ADMIN_USERNAME`/`API_TOKEN` set in the child process — auth
    enforced via that configured-admin identity (issue #28), not the
    always-auto-generated bootstrap identity (see
    `test_bootstrap_token_is_generated_printed_once_and_authenticates` for that one).
    """
    with _launch_server(
        tmp_path,
        env={
            "ANSINA_SECURITY__ADMIN_USERNAME": _E2E_ADMIN_USERNAME,
            "ANSINA_SECURITY__API_TOKEN": _E2E_TOKEN,
        },
    ) as srv:
        yield srv.base_url


_E2E_OIDC_CLIENT_ID = "e2e-oidc-client"
_E2E_OIDC_CLIENT_SECRET = "e2e-oidc-client-secret-value"


@pytest.fixture
def mock_idp() -> Iterator[MockIdp]:
    """A real OIDC IdP on loopback (issue #43) — see `MockIdp`'s own docstring for
    why this, not just another fake, is what actually proves the login exchange
    against a real (if local) HTTP server.
    """
    with _mock_idp() as idp:
        yield idp


@pytest.fixture
def oidc_server(tmp_path: Path, mock_idp: MockIdp) -> Iterator[str]:
    """Auth enforced (configured admin, issue #28) *and* OIDC enabled against
    `mock_idp` — the combination issue #43's own e2e test needs: an Admin to create
    role mappings with, plus a real IdP to log in against. `redirect_uri` is never
    actually dialed — this test drives `GET /auth/oidc/callback` directly rather
    than following a browser redirect, the same way a real browser flow's final hop
    would land on it.
    """
    with _launch_server(
        tmp_path,
        env={
            "ANSINA_SECURITY__ADMIN_USERNAME": _E2E_ADMIN_USERNAME,
            "ANSINA_SECURITY__API_TOKEN": _E2E_TOKEN,
            "ANSINA_SECURITY__OIDC__ENABLED": "true",
            "ANSINA_SECURITY__OIDC__ISSUER": mock_idp.issuer,
            "ANSINA_SECURITY__OIDC__CLIENT_ID": _E2E_OIDC_CLIENT_ID,
            "ANSINA_SECURITY__OIDC__CLIENT_SECRET": _E2E_OIDC_CLIENT_SECRET,
            "ANSINA_SECURITY__OIDC__REDIRECT_URI": "http://127.0.0.1:9/callback",
        },
    ) as srv:
        yield srv.base_url


def test_healthz(server: str) -> None:
    response = httpx.get(f"{server}/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz(server: str) -> None:
    response = httpx.get(f"{server}/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] is True


def test_readyz_has_no_heart_key_when_heart_is_disabled(server: str) -> None:
    """`[heart] enabled` defaults to `false` (issue #10) — the default boot must be
    entirely unaffected: no `heart` readiness key at all, not even a `false` one.
    """
    response = httpx.get(f"{server}/readyz")

    assert "heart" not in response.json()["checks"]


def test_version(server: str) -> None:
    response = httpx.get(f"{server}/version")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "ansina"
    assert body["version"]


def test_openapi_schema(server: str) -> None:
    response = httpx.get(f"{server}/openapi.json")

    assert response.status_code == 200
    assert set(response.json()["paths"]) == {
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
        "/auth/users/{user_id}/tokens/{token_id}",
        "/auth/users/{user_id}/totp",
        "/auth/users/{user_id}/roles/{role_id}",
        "/auth/groups",
        "/auth/groups/{group_id}",
        "/auth/groups/{group_id}/members/{user_id}",
        "/auth/groups/{group_id}/roles/{role_id}",
        "/auth/roles",
        "/auth/roles/{role_id}",
        "/auth/role-mappings",
        "/auth/role-mappings/{mapping_id}",
        "/auth/permissions",
        "/auth/me",
        "/auth/me/tokens",
        "/auth/me/tokens/{token_id}",
        "/auth/me/totp",
        "/auth/me/password",
        "/auth/oidc/login",
        "/auth/oidc/callback",
    }


def test_docs_and_redoc_are_gone(server: str) -> None:
    """Issue #25: `create_app` disables FastAPI's default `/docs`/`/redoc`/`/docs/
    oauth2-redirect` — plain Starlette routes that can't carry a `require(...)`
    authorization declaration, and non-functional with auth enabled regardless (no
    `fastapi.security` scheme is declared for Swagger UI's "Authorize" button to use).
    `/openapi.json` (`test_openapi_schema`) is the one FastAPI default kept, re-served
    as a gated route of our own.
    """
    assert httpx.get(f"{server}/docs").status_code == 404
    assert httpx.get(f"{server}/redoc").status_code == 404
    assert httpx.get(f"{server}/docs/oauth2-redirect").status_code == 404


def test_request_id_is_echoed(server: str) -> None:
    response = httpx.get(f"{server}/healthz", headers={"X-Request-ID": "e2e-trace"})

    assert response.headers["x-request-id"] == "e2e-trace"


def test_unknown_path_is_problem_json(server: str) -> None:
    response = httpx.get(f"{server}/nope")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "ansina.not_found"


def test_heart_tick_503_when_heart_disabled(server: str) -> None:
    """`[heart] enabled` defaults to `false` (issue #10) — with no Heart there is no
    tick loop (issue #11) either, so every `/heart/tick*` route must answer 503
    `problem+json` rather than pretending a loop exists.
    """
    response = httpx.get(f"{server}/heart/tick")

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "ansina.heart.disabled"


def test_authed_heart_tick_requires_token(authed_server: str) -> None:
    response = httpx.get(f"{authed_server}/heart/tick")

    assert response.status_code == 401
    assert response.json()["code"] == "ansina.unauthorized"


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


def _seed_read_role_user(db_path: Path) -> None:
    """Seed a `Read`-role user (`e2e-reader`) with an api_token credential directly
    into the running server's own SQLite file — the technique
    `test_migration_survives_a_restart` already uses, plus `ansina.auth.hashing` for
    the credential hash (see this module's docstring for why that one import is
    allowed). Shared by every e2e test that needs a non-bootstrap `Read` identity.
    """
    salt = new_token_salt()
    token_hash = hash_token(_E2E_READ_TOKEN, salt)
    with sqlite3.connect(db_path) as conn:
        user_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO users (id, username) VALUES (?, ?)", (user_id, "e2e-reader")
        )
        (role_id,) = conn.execute("SELECT id FROM roles WHERE slug = 'read'").fetchone()
        conn.execute(
            "INSERT INTO role_assignments (id, subject_type, subject_id, role_id) "
            "VALUES (?, 'user', ?, ?)",
            (uuid.uuid4().hex, user_id, role_id),
        )
        conn.execute(
            "INSERT INTO credentials (id, user_id, type, hash, salt) "
            "VALUES (?, ?, 'api_token', ?, ?)",
            (uuid.uuid4().hex, user_id, token_hash, salt),
        )
        conn.commit()


def test_read_role_token_progresses_401_then_403_then_200(
    authed_server: str, tmp_path: Path
) -> None:
    """Issue #25's acceptance criterion, black-box end to end: no token is 401, a
    `Read`-role token gets 403 on a mutating route it holds no grant for, and 200 on a
    `GET` it does. `tmp_path` is the same directory `authed_server`'s own fixture
    already launched the server against (pytest caches a function-scoped fixture once
    per test), so `ansina.db` is the real file the running process is reading and
    writing.
    """
    _seed_read_role_user(tmp_path / "ansina.db")

    # 401: no token at all.
    no_token_response = httpx.post(f"{authed_server}/heart/tick/pause")
    assert no_token_response.status_code == 401
    assert no_token_response.json()["code"] == "ansina.unauthorized"

    # 403: a valid Read-role token, but Read holds no grant for POST.
    read_headers = {"Authorization": f"Bearer {_E2E_READ_TOKEN}"}
    forbidden_response = httpx.post(
        f"{authed_server}/heart/tick/pause", headers=read_headers
    )
    assert forbidden_response.status_code == 403
    assert forbidden_response.headers["content-type"] == "application/problem+json"
    assert forbidden_response.json()["code"] == "ansina.forbidden"

    # 200: the same token, on the GET verb Read does grant.
    ok_response = httpx.get(f"{authed_server}/version", headers=read_headers)
    assert ok_response.status_code == 200
    assert ok_response.json()["name"] == "ansina"


def test_read_role_token_can_read_its_own_identity(
    authed_server: str, tmp_path: Path
) -> None:
    """Issue #30's headline AC, black-box end to end: a `Read`-role token — forbidden
    from every other `auth.*` route — gets 200 from `GET /auth/me` with its own
    identity and roles, no sudo grant involved.
    """
    _seed_read_role_user(tmp_path / "ansina.db")
    read_headers = {"Authorization": f"Bearer {_E2E_READ_TOKEN}"}

    response = httpx.get(f"{authed_server}/auth/me", headers=read_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "e2e-reader"
    assert body["roles"] == ["read"]
    assert body["sudo_active"] is False


def test_sudo_step_up_round_trip(authed_server: str, tmp_path: Path) -> None:
    """Issue #26's headline AC, black-box end to end: a `Maintain` caller is refused
    the sensitive break-glass route with no grant (403 `ansina.auth.sudo_required`),
    obtains one via `POST /auth/sudo`, succeeds with it (204), and is refused again
    once that grant is revoked — while an `Admin` token reaches the same route with no
    grant at all. `Maintain`/password seeded directly into the running server's SQLite
    file, the same technique `test_read_role_token_progresses_401_then_403_then_200`
    already establishes.
    """
    db_path = tmp_path / "ansina.db"
    salt = new_token_salt()
    token_hash = hash_token(_E2E_MAINTAIN_TOKEN, salt)
    password_hash = hash_password(_E2E_MAINTAIN_PASSWORD, _E2E_CHEAP_ARGON2)
    with sqlite3.connect(db_path) as conn:
        user_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO users (id, username) VALUES (?, ?)",
            (user_id, "e2e-maintainer"),
        )
        (role_id,) = conn.execute(
            "SELECT id FROM roles WHERE slug = 'maintain'"
        ).fetchone()
        conn.execute(
            "INSERT INTO role_assignments (id, subject_type, subject_id, role_id) "
            "VALUES (?, 'user', ?, ?)",
            (uuid.uuid4().hex, user_id, role_id),
        )
        conn.execute(
            "INSERT INTO credentials (id, user_id, type, hash, salt) "
            "VALUES (?, ?, 'api_token', ?, ?)",
            (uuid.uuid4().hex, user_id, token_hash, salt),
        )
        conn.execute(
            "INSERT INTO credentials (id, user_id, type, hash, salt) "
            "VALUES (?, ?, 'password', ?, NULL)",
            (uuid.uuid4().hex, user_id, password_hash),
        )
        conn.commit()

    maintain_headers = {"Authorization": f"Bearer {_E2E_MAINTAIN_TOKEN}"}

    # 403: Maintain's role grants DELETE on auth.sudo.grants, but there's no live
    # sudo grant yet.
    no_grant_response = httpx.delete(
        f"{authed_server}/auth/sudo/grants", headers=maintain_headers
    )
    assert no_grant_response.status_code == 403
    assert no_grant_response.json()["code"] == "ansina.auth.sudo_required"

    # Step up.
    step_up_response = httpx.post(
        f"{authed_server}/auth/sudo",
        headers=maintain_headers,
        json={"password": _E2E_MAINTAIN_PASSWORD},
    )
    assert step_up_response.status_code == 200
    grant_token = step_up_response.json()["token"]

    # 204: the same sensitive route, now presenting a live grant.
    granted_headers = {**maintain_headers, "X-Sudo-Token": grant_token}
    revoke_all_response = httpx.delete(
        f"{authed_server}/auth/sudo/grants", headers=granted_headers
    )
    assert revoke_all_response.status_code == 204

    # The break-glass call above revoked every active grant, including the one that
    # just authorized it — presenting it again is refused.
    again_response = httpx.delete(
        f"{authed_server}/auth/sudo/grants", headers=granted_headers
    )
    assert again_response.status_code == 403
    assert again_response.json()["code"] == "ansina.auth.sudo_required"

    # Admin (the configured admin `authed_server` already authenticates as via
    # _E2E_TOKEN — issue #28, unrelated to the bootstrap identity) reaches the same
    # sensitive route with no grant at all, by design.
    admin_response = httpx.delete(
        f"{authed_server}/auth/sudo/grants",
        headers={"Authorization": f"Bearer {_E2E_TOKEN}"},
    )
    assert admin_response.status_code == 204


def test_rbac_management_round_trip(authed_server: str) -> None:
    """Issue #27's headline flow, black-box end to end. Every other test above that
    needs a non-bootstrap identity (`test_read_role_token_progresses_401_then_403_
    then_200`, `test_sudo_step_up_round_trip`) seeds one directly into the running
    server's SQLite file, because until this issue there was no other way to create
    one. This test creates its `Maintain` user entirely over HTTP instead — the
    point of shipping this management API at all. The configured admin
    (`_E2E_TOKEN`, issue #28) creates a user, sets its password, issues it an API
    token, and assigns it `Maintain`; that user is then refused a mutating `/auth/*`
    call with no sudo grant, succeeds once stepped up, is refused an attempt to
    self-escalate to `Admin`, and — once it's the sole remaining Admin (this fixture
    seeds two: the bootstrap identity and the configured admin, so the configured
    admin's own direct grant is stripped first, via the sudo'd Maintain itself, to
    reach that state) — the bootstrap Admin cannot be deleted.
    """
    admin_headers = {"Authorization": f"Bearer {_E2E_TOKEN}"}

    created = httpx.post(
        f"{authed_server}/auth/users",
        headers=admin_headers,
        json={"username": "e2e-rbac-user"},
    )
    assert created.status_code == 201
    user_id = created.json()["id"]

    set_password = httpx.put(
        f"{authed_server}/auth/users/{user_id}/password",
        headers=admin_headers,
        json={"password": "a perfectly good passphrase"},
    )
    assert set_password.status_code == 204

    issued = httpx.post(
        f"{authed_server}/auth/users/{user_id}/tokens",
        headers=admin_headers,
        json={"label": "e2e"},
    )
    assert issued.status_code == 201
    user_token = issued.json()["token"]

    roles = httpx.get(f"{authed_server}/auth/roles", headers=admin_headers)
    assert roles.status_code == 200
    maintain_role_id = next(r["id"] for r in roles.json() if r["slug"] == "maintain")
    admin_role_id = next(r["id"] for r in roles.json() if r["slug"] == "admin")

    assign_maintain = httpx.post(
        f"{authed_server}/auth/users/{user_id}/roles/{maintain_role_id}",
        headers=admin_headers,
    )
    assert assign_maintain.status_code == 204

    user_headers = {"Authorization": f"Bearer {user_token}"}

    # 403 sudo_required: Maintain holds the grant, but hasn't stepped up yet.
    no_grant = httpx.post(
        f"{authed_server}/auth/users", headers=user_headers, json={"username": "nobody"}
    )
    assert no_grant.status_code == 403
    assert no_grant.json()["code"] == "ansina.auth.sudo_required"

    step_up = httpx.post(
        f"{authed_server}/auth/sudo",
        headers=user_headers,
        json={"password": "a perfectly good passphrase"},
    )
    assert step_up.status_code == 200
    granted_headers = {**user_headers, "X-Sudo-Token": step_up.json()["token"]}

    with_grant = httpx.post(
        f"{authed_server}/auth/users",
        headers=granted_headers,
        json={"username": "e2e-second-user"},
    )
    assert with_grant.status_code == 201

    # A sudo'd Maintain still cannot self-escalate to Admin.
    self_escalate = httpx.post(
        f"{authed_server}/auth/users/{user_id}/roles/{admin_role_id}",
        headers=granted_headers,
    )
    assert self_escalate.status_code == 403
    assert self_escalate.json()["code"] == "ansina.auth.self_escalation"

    # `authed_server` seeds two Admins (issue #28): the bootstrap identity and the
    # configured admin `_E2E_TOKEN` itself authenticates as — so deleting bootstrap
    # while the configured admin still holds its own direct grant would not be
    # refused, the other one remains. Strip the configured admin's own grant first
    # (via the sudo'd Maintain actor already on hand — using `admin_headers` itself
    # would also invalidate `admin_headers` for the very next call) so bootstrap
    # really is the sole remaining Admin, the scenario this assertion is about.
    admins = httpx.get(f"{authed_server}/auth/users", headers=admin_headers)
    bootstrap_id = next(
        u["id"] for u in admins.json() if u["username"] == "bootstrap-admin"
    )
    configured_admin_id = next(
        u["id"] for u in admins.json() if u["username"] == _E2E_ADMIN_USERNAME
    )
    strip_configured_admin = httpx.delete(
        f"{authed_server}/auth/users/{configured_admin_id}/roles/{admin_role_id}",
        headers=granted_headers,
    )
    assert strip_configured_admin.status_code == 204

    delete_last_admin = httpx.delete(
        f"{authed_server}/auth/users/{bootstrap_id}", headers=granted_headers
    )
    assert delete_last_admin.status_code == 409
    assert delete_last_admin.json()["code"] == "ansina.auth.last_admin"


def test_custom_role_round_trip(authed_server: str) -> None:
    """Issue #40's headline flow, black-box end to end: the configured admin creates
    a custom role granting `heart.tick`, a sudo'd `Maintain` edits its grant set
    (refused first with no sudo grant, per issue #37's fail-closed gate), the role is
    assigned to a fresh user who then actually reaches `GET /heart/tick` under it
    (503 `heart.disabled`, not 403 — proof the grant is live, not just stored), and
    deleting the role while still assigned is refused (409 `ansina.auth.role_in_use`)
    until it's detached.
    """
    admin_headers = {"Authorization": f"Bearer {_E2E_TOKEN}"}

    created_role = httpx.post(
        f"{authed_server}/auth/roles",
        headers=admin_headers,
        json={
            "slug": "e2e-heart-watcher",
            "name": "Heart Watcher",
            "permissions": [{"resource": "heart.tick", "verb": "GET"}],
        },
    )
    assert created_role.status_code == 201
    role_id = created_role.json()["id"]

    # A fresh Maintain user, created entirely over HTTP (same technique
    # `test_rbac_management_round_trip` uses) to edit the role's grants.
    maintainer = httpx.post(
        f"{authed_server}/auth/users",
        headers=admin_headers,
        json={"username": "e2e-role-editor"},
    )
    assert maintainer.status_code == 201
    maintainer_id = maintainer.json()["id"]
    httpx.put(
        f"{authed_server}/auth/users/{maintainer_id}/password",
        headers=admin_headers,
        json={"password": "a perfectly good passphrase"},
    )
    maintainer_token = httpx.post(
        f"{authed_server}/auth/users/{maintainer_id}/tokens",
        headers=admin_headers,
        json={"label": "e2e"},
    ).json()["token"]

    all_roles = httpx.get(f"{authed_server}/auth/roles", headers=admin_headers).json()
    maintain_role_id = next(r["id"] for r in all_roles if r["slug"] == "maintain")
    httpx.post(
        f"{authed_server}/auth/users/{maintainer_id}/roles/{maintain_role_id}",
        headers=admin_headers,
    )

    maintainer_headers = {"Authorization": f"Bearer {maintainer_token}"}
    no_grant_edit = httpx.patch(
        f"{authed_server}/auth/roles/{role_id}",
        headers=maintainer_headers,
        json={"permissions": [{"resource": "heart.tick", "verb": "GET"}]},
    )
    assert no_grant_edit.status_code == 403
    assert no_grant_edit.json()["code"] == "ansina.auth.sudo_required"

    step_up = httpx.post(
        f"{authed_server}/auth/sudo",
        headers=maintainer_headers,
        json={"password": "a perfectly good passphrase"},
    )
    assert step_up.status_code == 200
    granted_headers = {**maintainer_headers, "X-Sudo-Token": step_up.json()["token"]}

    edited = httpx.patch(
        f"{authed_server}/auth/roles/{role_id}",
        headers=granted_headers,
        json={
            "permissions": [
                {"resource": "heart.tick", "verb": "GET"},
                {"resource": "heart.tick", "verb": "POST"},
            ]
        },
    )
    assert edited.status_code == 200
    assert {(g["resource"], g["verb"]) for g in edited.json()["permissions"]} == {
        ("heart.tick", "GET"),
        ("heart.tick", "POST"),
    }

    # Assign the custom role to a fresh, otherwise-unprivileged user and prove the
    # grant is actually live: 503 heart.disabled (authorization passed), not 403.
    holder = httpx.post(
        f"{authed_server}/auth/users",
        headers=admin_headers,
        json={"username": "e2e-role-holder"},
    )
    assert holder.status_code == 201
    holder_id = holder.json()["id"]
    holder_token = httpx.post(
        f"{authed_server}/auth/users/{holder_id}/tokens",
        headers=admin_headers,
        json={"label": "e2e"},
    ).json()["token"]
    assign_custom_role = httpx.post(
        f"{authed_server}/auth/users/{holder_id}/roles/{role_id}",
        headers=admin_headers,
    )
    assert assign_custom_role.status_code == 204

    holder_headers = {"Authorization": f"Bearer {holder_token}"}
    tick_response = httpx.get(f"{authed_server}/heart/tick", headers=holder_headers)
    assert tick_response.status_code == 503
    assert tick_response.json()["code"] == "ansina.heart.disabled"

    # Still assigned: delete is refused.
    delete_while_assigned = httpx.delete(
        f"{authed_server}/auth/roles/{role_id}", headers=admin_headers
    )
    assert delete_while_assigned.status_code == 409
    assert delete_while_assigned.json()["code"] == "ansina.auth.role_in_use"

    # Detach, then delete succeeds.
    detach = httpx.delete(
        f"{authed_server}/auth/users/{holder_id}/roles/{role_id}",
        headers=admin_headers,
    )
    assert detach.status_code == 204
    delete_response = httpx.delete(
        f"{authed_server}/auth/roles/{role_id}", headers=admin_headers
    )
    assert delete_response.status_code == 204


def test_role_mapping_round_trip(authed_server: str) -> None:
    """Issue #42's headline flow, black-box end to end: the configured admin creates
    a custom role, maps an IdP claim onto it, sees it listed, a duplicate submission
    is refused (409), and deleting it removes it from the catalog.
    """
    admin_headers = {"Authorization": f"Bearer {_E2E_TOKEN}"}

    created_role = httpx.post(
        f"{authed_server}/auth/roles",
        headers=admin_headers,
        json={"slug": "e2e-ops", "name": "Ops"},
    )
    assert created_role.status_code == 201
    role_id = created_role.json()["id"]

    mapping_payload = {
        "provider": "acme",
        "claim": "groups",
        "value": "ops-team",
        "role_id": role_id,
    }
    created_mapping = httpx.post(
        f"{authed_server}/auth/role-mappings",
        headers=admin_headers,
        json=mapping_payload,
    )
    assert created_mapping.status_code == 201
    mapping_id = created_mapping.json()["id"]

    listed = httpx.get(f"{authed_server}/auth/role-mappings", headers=admin_headers)
    assert listed.status_code == 200
    assert any(m["id"] == mapping_id for m in listed.json())

    duplicate = httpx.post(
        f"{authed_server}/auth/role-mappings",
        headers=admin_headers,
        json=mapping_payload,
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "ansina.auth.duplicate"

    deleted = httpx.delete(
        f"{authed_server}/auth/role-mappings/{mapping_id}", headers=admin_headers
    )
    assert deleted.status_code == 204

    after_delete = httpx.get(
        f"{authed_server}/auth/role-mappings", headers=admin_headers
    )
    assert all(m["id"] != mapping_id for m in after_delete.json())


def test_oidc_first_login_provisions_and_claims_refresh_updates_roles(
    oidc_server: str, mock_idp: MockIdp
) -> None:
    """Issue #43's own headline flow, black-box end to end against a real (if
    local) IdP process — not a fake, a second real HTTP server `python -m ansina`'s
    subprocess actually dials. Covers the issue's own acceptance criteria: first-
    login provisioning, reuse on a later login for the same subject, replay
    rejection, and AC #4 — a role granted from a first login's claims is revoked on
    a second login whose claims no longer match, without ever touching the
    configured admin's own (`source='local'`) role.
    """
    admin_headers = {"Authorization": f"Bearer {_E2E_TOKEN}"}

    roles = httpx.get(f"{oidc_server}/auth/roles", headers=admin_headers).json()
    read_role_id = next(r["id"] for r in roles if r["slug"] == "read")
    mapping = httpx.post(
        f"{oidc_server}/auth/role-mappings",
        headers=admin_headers,
        json={
            "provider": mock_idp.issuer,
            "claim": "groups",
            "value": "ops-team",
            "role_id": read_role_id,
        },
    )
    assert mapping.status_code == 201

    # First login: "groups" includes "ops-team" -> the mapped role is granted.
    login1 = httpx.post(f"{oidc_server}/auth/oidc/login")
    assert login1.status_code == 200
    login1_body = login1.json()
    nonce1 = _nonce_from_authorization_url(login1_body["authorization_url"])
    mock_idp.set_next_login(
        sub="e2e-oidc-subject",
        nonce=nonce1,
        extra_claims={"preferred_username": "e2e-oidc-user", "groups": ["ops-team"]},
    )

    callback1 = httpx.get(
        f"{oidc_server}/auth/oidc/callback",
        params={"code": "e2e-auth-code-1", "state": login1_body["state"]},
    )
    assert callback1.status_code == 200
    callback1_body = callback1.json()
    assert callback1_body["expires_at"] is not None
    token1 = callback1_body["token"]

    me1 = httpx.get(
        f"{oidc_server}/auth/me", headers={"Authorization": f"Bearer {token1}"}
    )
    assert me1.status_code == 200
    assert me1.json()["username"] == "e2e-oidc-user"
    assert me1.json()["roles"] == ["read"]

    # Exactly one user was provisioned, not one per login.
    users = httpx.get(f"{oidc_server}/auth/users", headers=admin_headers).json()
    assert sum(1 for u in users if u["username"] == "e2e-oidc-user") == 1

    # The `state` from the first login can never be redeemed twice.
    replayed = httpx.get(
        f"{oidc_server}/auth/oidc/callback",
        params={"code": "e2e-auth-code-1", "state": login1_body["state"]},
    )
    assert replayed.status_code == 400
    assert replayed.json()["code"] == "ansina.auth.oidc_state_invalid"

    # Second login, same subject, "groups" no longer includes "ops-team" -> AC #4:
    # the mapped role is revoked on this login, not just left stale until it expires.
    login2 = httpx.post(f"{oidc_server}/auth/oidc/login")
    login2_body = login2.json()
    nonce2 = _nonce_from_authorization_url(login2_body["authorization_url"])
    mock_idp.set_next_login(
        sub="e2e-oidc-subject",
        nonce=nonce2,
        extra_claims={"preferred_username": "e2e-oidc-user", "groups": []},
    )

    callback2 = httpx.get(
        f"{oidc_server}/auth/oidc/callback",
        params={"code": "e2e-auth-code-2", "state": login2_body["state"]},
    )
    assert callback2.status_code == 200
    token2 = callback2.json()["token"]

    me2 = httpx.get(
        f"{oidc_server}/auth/me", headers={"Authorization": f"Bearer {token2}"}
    )
    # The role granting `me.profile:GET` is gone — 403, not 200.
    assert me2.status_code == 403
    assert me2.json()["code"] == "ansina.forbidden"

    # The configured admin (`_E2E_TOKEN`, a `source='local'` assignment) is
    # unaffected by any of the above.
    still_admin = httpx.get(f"{oidc_server}/auth/me", headers=admin_headers)
    assert still_admin.status_code == 200
    assert "admin" in still_admin.json()["roles"]


def test_oidc_callback_rejects_a_forged_nonce(
    oidc_server: str, mock_idp: MockIdp
) -> None:
    """AC #2, end to end against the real subprocess: a mismatched nonce is rejected
    before any provisioning runs — no user is ever created for it.
    """
    login = httpx.post(f"{oidc_server}/auth/oidc/login")
    login_body = login.json()
    mock_idp.set_next_login(sub="e2e-forged-subject", nonce="not-the-real-nonce-at-all")

    response = httpx.get(
        f"{oidc_server}/auth/oidc/callback",
        params={"code": "e2e-forged-code", "state": login_body["state"]},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "ansina.auth.oidc_token_invalid"

    admin_headers = {"Authorization": f"Bearer {_E2E_TOKEN}"}
    users = httpx.get(f"{oidc_server}/auth/users", headers=admin_headers).json()
    assert all(u["username"] != "e2e-forged-subject" for u in users)


def test_self_service_token_round_trip(authed_server: str) -> None:
    """Issue #28's headline flow, black-box end to end: mint a token via
    `POST /auth/me/tokens`, authenticate a real request with it, watch
    `last_used_at` move off `null`, then revoke it and confirm it stops
    authenticating on the very next request.
    """
    admin_headers = {"Authorization": f"Bearer {_E2E_TOKEN}"}

    minted = httpx.post(
        f"{authed_server}/auth/me/tokens", headers=admin_headers, json={"label": "cli"}
    )
    assert minted.status_code == 201
    body = minted.json()
    assert body["last_used_at"] is None
    new_token = body["token"]
    token_id = body["id"]

    authed = httpx.get(
        f"{authed_server}/auth/me", headers={"Authorization": f"Bearer {new_token}"}
    )
    assert authed.status_code == 200

    listed = httpx.get(
        f"{authed_server}/auth/me/tokens",
        headers={"Authorization": f"Bearer {new_token}"},
    )
    assert listed.status_code == 200
    minted_entry = next(t for t in listed.json() if t["id"] == token_id)
    assert minted_entry["last_used_at"] is not None
    assert "hash" not in minted_entry
    assert "salt" not in minted_entry

    revoked = httpx.delete(
        f"{authed_server}/auth/me/tokens/{token_id}",
        headers={"Authorization": f"Bearer {new_token}"},
    )
    assert revoked.status_code == 204

    rejected = httpx.get(
        f"{authed_server}/auth/me", headers={"Authorization": f"Bearer {new_token}"}
    )
    assert rejected.status_code == 401
    assert rejected.json()["code"] == "ansina.unauthorized"


def test_bootstrap_and_configured_admin_are_fully_independent(
    tmp_path: Path,
) -> None:
    """Issue #28: one boot with both mechanisms configured produces two
    independently-authenticating identities — the printed bootstrap banner token and
    the env-supplied configured-admin token — and revoking one never affects the
    other.
    """
    output_path = tmp_path / "server-output.log"
    port = _free_port()
    (tmp_path / "ansina.toml").write_text(
        f'[server]\nhost = "127.0.0.1"\nport = {port}\n'
        f'[database]\npath = "{(tmp_path / "ansina.db").as_posix()}"\n',
        encoding="utf-8",
    )
    base_url = f"http://127.0.0.1:{port}"

    with output_path.open("w", encoding="utf-8") as output_file:
        process = subprocess.Popen(  # fixed argv, no shell, no untrusted input
            [sys.executable, "-m", "ansina"],
            cwd=tmp_path,
            stdout=output_file,
            stderr=subprocess.STDOUT,
            env={
                **os.environ,
                "ANSINA_SECURITY__ADMIN_USERNAME": _E2E_ADMIN_USERNAME,
                "ANSINA_SECURITY__API_TOKEN": _E2E_TOKEN,
            },
        )
        try:
            deadline = time.monotonic() + _STARTUP_TIMEOUT_S
            last_error: Exception | None = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(
                        f"ansina exited early (code {process.returncode}):\n"
                        f"{output_path.read_text(encoding='utf-8')}"
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

            output = output_path.read_text(encoding="utf-8")
            match = re.search(r"^   (\S+)$", output, re.MULTILINE)
            assert match is not None, f"bootstrap token banner not found:\n{output}"
            bootstrap_token = match.group(1)

            for token in (bootstrap_token, _E2E_TOKEN):
                response = httpx.get(
                    f"{base_url}/version",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 200

            # Revoking the configured admin's own token never touches bootstrap's.
            me = httpx.get(
                f"{base_url}/auth/me/tokens",
                headers={"Authorization": f"Bearer {_E2E_TOKEN}"},
            )
            configured_token_id = me.json()[0]["id"]
            revoked = httpx.delete(
                f"{base_url}/auth/me/tokens/{configured_token_id}",
                headers={"Authorization": f"Bearer {_E2E_TOKEN}"},
            )
            assert revoked.status_code == 204

            still_bootstrap = httpx.get(
                f"{base_url}/version",
                headers={"Authorization": f"Bearer {bootstrap_token}"},
            )
            assert still_bootstrap.status_code == 200

            now_rejected = httpx.get(
                f"{base_url}/version",
                headers={"Authorization": f"Bearer {_E2E_TOKEN}"},
            )
            assert now_rejected.status_code == 401
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_bootstrap_token_is_generated_printed_once_and_authenticates(
    tmp_path: Path,
) -> None:
    """Issue #24 (redesign): first boot with `security.enabled` at its default
    (`true`) and no `ANSINA_SECURITY__API_TOKEN` configured auto-generates a
    bootstrap token, prints it to stdout exactly once, and that token immediately
    authenticates a real request — the whole generate -> print -> DB-backed-verify
    chain working end to end, not just its pieces in isolation.

    Captures the child's combined stdout/stderr to a file rather than a pipe:
    reading a live process's stdout pipe risks blocking on buffering, where a file
    can be read at any time without coordinating with the writer.
    """
    port = _free_port()
    (tmp_path / "ansina.toml").write_text(
        f'[server]\nhost = "127.0.0.1"\nport = {port}\n'
        f'[database]\npath = "{(tmp_path / "ansina.db").as_posix()}"\n',
        encoding="utf-8",
    )
    output_path = tmp_path / "server-output.log"
    base_url = f"http://127.0.0.1:{port}"

    with output_path.open("w", encoding="utf-8") as output_file:
        process = subprocess.Popen(  # fixed argv, no shell, no untrusted input
            [sys.executable, "-m", "ansina"],
            cwd=tmp_path,
            stdout=output_file,
            stderr=subprocess.STDOUT,
            env=os.environ,
        )
        try:
            deadline = time.monotonic() + _STARTUP_TIMEOUT_S
            last_error: Exception | None = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(
                        f"ansina exited early (code {process.returncode}):\n"
                        f"{output_path.read_text(encoding='utf-8')}"
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

            output = output_path.read_text(encoding="utf-8")
            # The banner's token line (see `ansina.auth.bootstrap._BANNER`): exactly
            # three leading spaces, nothing else on the line.
            match = re.search(r"^   (\S+)$", output, re.MULTILINE)
            assert match is not None, f"bootstrap token banner not found:\n{output}"
            token = match.group(1)

            response = httpx.get(
                f"{base_url}/version", headers={"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 200
            assert response.json()["name"] == "ansina"

            response = httpx.get(
                f"{base_url}/version",
                headers={"Authorization": f"Bearer {token}x"},
            )
            assert response.status_code == 401

            # The raw token appears exactly once in the entire captured output — the
            # banner itself — never inside a JSON log line alongside it.
            assert output.count(token) == 1
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_migration_survives_a_restart(tmp_path: Path) -> None:
    """A real, black-box run of issue #6's acceptance criteria: on first boot the
    database file is created and migrated, and a second boot against the same file
    does not re-apply already-applied migrations — checked from outside the process,
    via a plain `sqlite3` connection to the file the server wrote.
    """
    with _launch_server(tmp_path) as srv:
        response = httpx.get(f"{srv.base_url}/readyz")
        assert response.json()["checks"]["database"] is True

    db_path = tmp_path / "ansina.db"
    assert db_path.exists()
    with sqlite3.connect(db_path) as conn:
        (journal_mode,) = conn.execute("PRAGMA journal_mode").fetchone()
        assert journal_mode.lower() == "wal"
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
        # (1,) = storage's own bookkeeping table (issue #6); (2,) = the RBAC identity
        # model (issue #24); (3,) = sudo grants/lockouts (issue #26); (4,) = the user
        # deletion tombstone (issue #27); (5,) = the resource-served-verbs column
        # (issue #38); (6,) = the totp credential type (issue #41); (7,) = role-mapping
        # provenance + the role_mappings unique index (issue #42); (8,) = the in-flight
        # OIDC login state table (issue #43) — bump this alongside `storage/
        # migrations/` whenever a new migration lands.
        assert rows == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,)]

    # Boot again against the same tmp_path (same ansina.toml, same db file).
    with _launch_server(tmp_path) as srv:
        response = httpx.get(f"{srv.base_url}/readyz")
        assert response.json()["checks"]["database"] is True

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
        # still exactly these rows — nothing re-applied
        assert rows == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,)]


def test_heart_enabled_without_a_viable_runtime_fails_loudly(tmp_path: Path) -> None:
    """Issue #10 AC #4: turning the Heart on with no viable adapter available must
    fail loudly at boot — never a silent no-op, never a bare traceback. This is
    platform-independent: CI never installs the `mlx` extra on either OS leg, so
    `mlx_lm` is never importable regardless of `sys.platform`. (Asserted on
    "non-zero exit + stderr mentions the heart," not the exact sentence, so this
    stays green on a Mac that *does* have the extra installed — there the failure is
    an absent model instead of an absent backend, per `ansina.heart.selection`.)
    """
    port = _free_port()
    (tmp_path / "ansina.toml").write_text(
        f'[server]\nhost = "127.0.0.1"\nport = {port}\n'
        f'[database]\npath = "{(tmp_path / "ansina.db").as_posix()}"\n',
        encoding="utf-8",
    )

    process = subprocess.run(
        [sys.executable, "-m", "ansina"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "ANSINA_HEART__ENABLED": "true"},
        timeout=_STARTUP_TIMEOUT_S,
    )

    assert process.returncode != 0
    combined = (process.stdout + process.stderr).lower()
    assert "heart" in combined
    assert "traceback" not in combined


def test_shuts_down_cleanly(tmp_path: Path) -> None:
    """Issue #16's M0 E2E coverage list includes "the process shuts down cleanly" —
    unlike every other test here, this one inspects the subprocess itself (exit code,
    captured output) rather than just its HTTP responses, since a stuck or crashing
    shutdown wouldn't show up in any response the server sent while still running.
    """
    with _launch_server(tmp_path) as srv:
        response = httpx.get(f"{srv.base_url}/healthz")
        assert response.status_code == 200
        process = srv.process

    # By the time `_launch_server`'s `finally` block returns control here, it has
    # already sent SIGTERM (`Popen.terminate()`) and waited for exit.
    assert process.returncode == -signal.SIGTERM
    output = process.stdout.read() if process.stdout else ""
    assert "Traceback" not in output
