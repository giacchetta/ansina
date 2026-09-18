"""Shared fixtures — the dependency-injected equivalent of Jest fixture files.

Requested by name in a test's signature and scoped per-test, rather than imported.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from ansina.auth.oidc import OidcHttpClient, OidcProviderError
from ansina.logging.formatter import JsonFormatter


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every ANSINA_* env var so a test never sees the dev machine's state."""
    for key in list(os.environ):
        if key.startswith("ANSINA_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def tmp_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Run the test from an empty temp dir so a real ./ansina.toml can't leak in."""
    monkeypatch.chdir(tmp_path)
    yield tmp_path


@pytest.fixture
def captured_logs() -> Iterator[Callable[[], list[dict[str, Any]]]]:
    """A root-logger handler writing `JsonFormatter` output to an in-memory buffer.

    Returns a callable that parses every line written so far as JSON — the assertion
    surface tests use instead of talking to stderr directly. Lives at the top level (not
    just under `tests/unit/logging/`) so `tests/unit/api/` can assert the request id
    lands in emitted log lines too.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)

    def _read() -> list[dict[str, Any]]:
        lines = stream.getvalue().splitlines()
        return [json.loads(line) for line in lines]

    try:
        yield _read
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


# --- issue #43: a fake OIDC IdP, served entirely through `OidcHttpClient`'s two
# methods — no network, no mock HTTP server. Lives at the top level (not just under
# `tests/unit/auth/`) so `tests/unit/api/routes/test_oidc.py` can build on the exact
# same fake IdP `tests/unit/auth/test_oidc.py`/`test_oidc_login.py` do, the same
# reasoning `captured_logs` above is already here for.

OIDC_ISSUER = "https://idp.test.example"
OIDC_CLIENT_ID = "ansina-test-client"
OIDC_KID = "test-signing-key"


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@pytest.fixture
def oidc_issuer() -> str:
    return OIDC_ISSUER


@pytest.fixture
def oidc_client_id() -> str:
    return OIDC_CLIENT_ID


@pytest.fixture
def idp_key() -> RSAPrivateKey:
    """The fake IdP's signing key — generated fresh per test, never a real secret."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def idp_jwks(idp_key: RSAPrivateKey) -> dict[str, Any]:
    """The JWKS document a real IdP would serve at its `jwks_uri`, holding exactly
    `idp_key`'s public half under `OIDC_KID`.
    """
    numbers = idp_key.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "kid": OIDC_KID,
                "alg": "RS256",
                "n": _b64url_uint(numbers.n),
                "e": _b64url_uint(numbers.e),
            }
        ]
    }


@pytest.fixture
def sign_id_token(idp_key: RSAPrivateKey) -> Callable[..., str]:
    """A factory minting a signed ID token as the fake IdP's token endpoint would
    return it — defaults to a fully valid token (`iss`/`aud`/`sub`/`exp`/`iat`/
    `nonce` all present, signed RS256 under `OIDC_KID` with `idp_key`), with every
    field overridable so each rejection case in `test_oidc.py` only has to state what
    it's deliberately breaking.
    """

    def _sign(
        *,
        sub: str = "user-123",
        nonce: str = "test-nonce",
        issuer: str = OIDC_ISSUER,
        audience: str = OIDC_CLIENT_ID,
        exp_offset: float = 300.0,
        extra_claims: Mapping[str, Any] | None = None,
        key: RSAPrivateKey | None = None,
        kid: str | None = OIDC_KID,
        algorithm: str = "RS256",
        omit_nonce: bool = False,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": issuer,
            "aud": audience,
            "sub": sub,
            "exp": now + int(exp_offset),
            "iat": now,
        }
        if not omit_nonce:
            claims["nonce"] = nonce
        if extra_claims:
            claims.update(extra_claims)
        headers = {"kid": kid} if kid is not None else {}
        signing_key = key if key is not None else idp_key
        return jwt.encode(claims, signing_key, algorithm=algorithm, headers=headers)

    return _sign


class FakeOidcHttpClient:
    """An `OidcHttpClient` implementation serving a synthetic discovery document,
    JWKS, and token-endpoint response — no network, no mock HTTP server. Every
    outbound call this module's functions make is captured for assertion
    (`post_calls`), and any of the three responses can be swapped for an
    `OidcProviderError` to exercise a transport-failure path.
    """

    def __init__(
        self,
        *,
        issuer: str = OIDC_ISSUER,
        jwks: Mapping[str, Any] | None = None,
        token_response: Mapping[str, Any] | None = None,
        discovery_overrides: Mapping[str, Any] | None = None,
        discovery_error: Exception | None = None,
        jwks_error: Exception | None = None,
        token_error: Exception | None = None,
    ) -> None:
        discovery: dict[str, Any] = {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/authorize",
            "token_endpoint": f"{issuer}/token",
            "jwks_uri": f"{issuer}/jwks",
        }
        if discovery_overrides:
            discovery.update(discovery_overrides)
        self._issuer = issuer
        self._discovery = discovery
        self._jwks = dict(jwks) if jwks is not None else {"keys": []}
        self._token_response = dict(token_response) if token_response else {}
        self._discovery_error = discovery_error
        self._jwks_error = jwks_error
        self._token_error = token_error
        self.post_calls: list[tuple[str, dict[str, str], tuple[str, str]]] = []

    def get_json(self, url: str) -> dict[str, Any]:
        if url == f"{self._issuer}/.well-known/openid-configuration":
            if self._discovery_error is not None:
                raise self._discovery_error
            return self._discovery
        if url == f"{self._issuer}/jwks":
            if self._jwks_error is not None:
                raise self._jwks_error
            return self._jwks
        raise AssertionError(f"FakeOidcHttpClient.get_json: unexpected url {url!r}")

    def post_form(
        self, url: str, form: Mapping[str, str], *, auth: tuple[str, str]
    ) -> dict[str, Any]:
        self.post_calls.append((url, dict(form), auth))
        if self._token_error is not None:
            raise self._token_error
        return self._token_response


@pytest.fixture
def fake_idp_client_factory() -> type[FakeOidcHttpClient]:
    """Exposes the class itself — most tests want full control over construction
    (which fields to break), so a factory *function* fixture would only add a layer
    of indirection over just importing/using the class directly, and cross-file
    imports aren't reliable under this suite's `--import-mode=importlib` (`tests/`
    has no `__init__.py`) — a fixture is how a shared test helper crosses file
    boundaries here (see this module's own docstring).
    """
    return FakeOidcHttpClient


@pytest.fixture
def unreachable_client() -> OidcHttpClient:
    """An `OidcHttpClient` whose every call raises `OidcProviderError` — the
    "IdP is completely unreachable" case, distinct from `FakeOidcHttpClient`'s
    per-call error injection (which still answers *some* calls normally).
    """

    class _Unreachable:
        def get_json(self, url: str) -> dict[str, Any]:
            raise OidcProviderError(f"connection refused: {url}")

        def post_form(
            self, url: str, form: Mapping[str, str], *, auth: tuple[str, str]
        ) -> dict[str, Any]:
            raise OidcProviderError(f"connection refused: {url}")

    return _Unreachable()
