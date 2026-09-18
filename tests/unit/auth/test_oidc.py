"""Tests for `ansina.auth.oidc` — discovery, JWKS, token exchange, ID-token
validation, and PKCE. See issue #43.

No network and no mock HTTP server anywhere here: every test drives these pure
functions through `fake_idp_client_factory` (`tests/conftest.py`) or, for
`validate_id_token` itself, real signed JWTs built by `sign_id_token` against a real
(but throwaway, per-test) RSA key — the actual `PyJWT` verification path runs, just
never over a socket.

Every fixture used here (`idp_key`, `idp_jwks`, `sign_id_token`,
`fake_idp_client_factory`, `oidc_issuer`, `oidc_client_id`) is defined in the
suite-wide `tests/conftest.py`, never imported directly — `tests/` has no
`__init__.py`, so a plain `from tests.conftest import ...` isn't the supported way to
share a test helper across files here (see that module's own docstring).
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from ansina.auth.oidc import (
    HttpxOidcClient,
    OidcHttpClient,
    OidcProviderError,
    OidcTokenError,
    ProviderMetadata,
    _parse_json_object,
    code_challenge,
    discover,
    exchange_code,
    fetch_jwks,
    new_code_verifier,
    validate_id_token,
)

FakeClientFactory = Callable[..., OidcHttpClient]

# --- discover -----------------------------------------------------------------------


def test_discover_returns_metadata_from_a_valid_document(
    fake_idp_client_factory: FakeClientFactory, oidc_issuer: str
) -> None:
    client = fake_idp_client_factory(issuer=oidc_issuer)

    metadata = discover(client, oidc_issuer)

    assert metadata == ProviderMetadata(
        issuer=oidc_issuer,
        authorization_endpoint=f"{oidc_issuer}/authorize",
        token_endpoint=f"{oidc_issuer}/token",
        jwks_uri=f"{oidc_issuer}/jwks",
    )


def test_discover_strips_a_trailing_slash_before_comparing_issuers(
    fake_idp_client_factory: FakeClientFactory, oidc_issuer: str
) -> None:
    """A caller passing a trailing-slash issuer (e.g. an un-normalized raw string —
    `OidcSettings._strip_trailing_slash` normalizes the config path, but `discover`
    is defensive on its own too) must still match a well-formed, slash-free IdP.
    """
    client = fake_idp_client_factory(issuer=oidc_issuer)

    metadata = discover(client, f"{oidc_issuer}/")

    assert metadata.issuer == oidc_issuer


def test_discover_rejects_a_mismatched_issuer_in_the_document(
    fake_idp_client_factory: FakeClientFactory, oidc_issuer: str
) -> None:
    client = fake_idp_client_factory(
        issuer=oidc_issuer,
        discovery_overrides={"issuer": "https://not-the-configured-issuer.example"},
    )

    with pytest.raises(OidcProviderError, match="advertises issuer"):
        discover(client, oidc_issuer)


@pytest.mark.parametrize(
    "missing_field",
    ["authorization_endpoint", "token_endpoint", "jwks_uri"],
)
def test_discover_rejects_a_document_missing_a_required_field(
    fake_idp_client_factory: FakeClientFactory, oidc_issuer: str, missing_field: str
) -> None:
    client = fake_idp_client_factory(
        issuer=oidc_issuer, discovery_overrides={missing_field: ""}
    )

    with pytest.raises(OidcProviderError, match="missing required field"):
        discover(client, oidc_issuer)


def test_discover_wraps_a_transport_failure(
    fake_idp_client_factory: FakeClientFactory, oidc_issuer: str
) -> None:
    client = fake_idp_client_factory(
        issuer=oidc_issuer, discovery_error=OidcProviderError("boom")
    )

    with pytest.raises(OidcProviderError, match="boom"):
        discover(client, oidc_issuer)


# --- fetch_jwks ----------------------------------------------------------------------


def test_fetch_jwks_parses_a_valid_document(
    fake_idp_client_factory: FakeClientFactory,
    oidc_issuer: str,
    idp_jwks: dict[str, object],
) -> None:
    client = fake_idp_client_factory(issuer=oidc_issuer, jwks=idp_jwks)

    jwks = fetch_jwks(client, f"{oidc_issuer}/jwks")

    assert len(jwks.keys) == 1


def test_fetch_jwks_rejects_a_document_with_no_keys(
    fake_idp_client_factory: FakeClientFactory, oidc_issuer: str
) -> None:
    """`{"keys": []}` — well-formed JSON, but `PyJWKSet` itself refuses an empty key
    set (`PyJWKSetError`, a *sibling* of `PyJWKError` under `PyJWTError`, not a
    subclass of it — catching only `PyJWKError` would let this one escape unmapped).
    """
    client = fake_idp_client_factory(issuer=oidc_issuer, jwks={"keys": []})

    with pytest.raises(OidcProviderError, match="invalid JWKS"):
        fetch_jwks(client, f"{oidc_issuer}/jwks")


def test_fetch_jwks_rejects_a_key_with_an_invalid_shape(
    fake_idp_client_factory: FakeClientFactory, oidc_issuer: str
) -> None:
    client = fake_idp_client_factory(
        issuer=oidc_issuer, jwks={"keys": [{"kty": "not-a-real-key-type"}]}
    )

    with pytest.raises(OidcProviderError, match="invalid JWKS"):
        fetch_jwks(client, f"{oidc_issuer}/jwks")


def test_fetch_jwks_wraps_a_transport_failure(
    fake_idp_client_factory: FakeClientFactory, oidc_issuer: str
) -> None:
    client = fake_idp_client_factory(
        issuer=oidc_issuer, jwks_error=OidcProviderError("unreachable")
    )

    with pytest.raises(OidcProviderError, match="unreachable"):
        fetch_jwks(client, f"{oidc_issuer}/jwks")


# --- exchange_code --------------------------------------------------------------------


@pytest.fixture
def metadata(oidc_issuer: str) -> ProviderMetadata:
    return ProviderMetadata(
        issuer=oidc_issuer,
        authorization_endpoint=f"{oidc_issuer}/authorize",
        token_endpoint=f"{oidc_issuer}/token",
        jwks_uri=f"{oidc_issuer}/jwks",
    )


def test_exchange_code_returns_the_id_token(
    fake_idp_client_factory: FakeClientFactory,
    oidc_issuer: str,
    oidc_client_id: str,
    metadata: ProviderMetadata,
    sign_id_token: Callable[..., str],
) -> None:
    id_token = sign_id_token()
    client = fake_idp_client_factory(
        issuer=oidc_issuer, token_response={"id_token": id_token}
    )

    result = exchange_code(
        client,
        metadata,
        client_id=oidc_client_id,
        client_secret="test-secret",
        redirect_uri="https://ansina.test/callback",
        code="auth-code",
        code_verifier="verifier-value",
    )

    assert result == id_token


def test_exchange_code_sends_the_expected_form_and_basic_auth(
    fake_idp_client_factory: FakeClientFactory,
    oidc_issuer: str,
    oidc_client_id: str,
    metadata: ProviderMetadata,
) -> None:
    client = fake_idp_client_factory(
        issuer=oidc_issuer, token_response={"id_token": "unused"}
    )
    assert hasattr(client, "post_calls")

    exchange_code(
        client,
        metadata,
        client_id=oidc_client_id,
        client_secret="test-secret",
        redirect_uri="https://ansina.test/callback",
        code="auth-code",
        code_verifier="verifier-value",
    )

    post_calls = client.post_calls
    assert len(post_calls) == 1
    url, form, auth = post_calls[0]
    assert url == f"{oidc_issuer}/token"
    assert form == {
        "grant_type": "authorization_code",
        "code": "auth-code",
        "redirect_uri": "https://ansina.test/callback",
        "client_id": oidc_client_id,
        "code_verifier": "verifier-value",
    }
    assert auth == (oidc_client_id, "test-secret")


def test_exchange_code_rejects_a_response_with_no_id_token(
    fake_idp_client_factory: FakeClientFactory,
    oidc_issuer: str,
    oidc_client_id: str,
    metadata: ProviderMetadata,
) -> None:
    client = fake_idp_client_factory(
        issuer=oidc_issuer, token_response={"access_token": "only-this"}
    )

    with pytest.raises(OidcTokenError, match="carried no id_token"):
        exchange_code(
            client,
            metadata,
            client_id=oidc_client_id,
            client_secret="test-secret",
            redirect_uri="https://ansina.test/callback",
            code="auth-code",
            code_verifier="verifier-value",
        )


def test_exchange_code_wraps_a_transport_failure(
    fake_idp_client_factory: FakeClientFactory,
    oidc_issuer: str,
    oidc_client_id: str,
    metadata: ProviderMetadata,
) -> None:
    client = fake_idp_client_factory(
        issuer=oidc_issuer, token_error=OidcProviderError("token endpoint down")
    )

    with pytest.raises(OidcProviderError, match="token endpoint down"):
        exchange_code(
            client,
            metadata,
            client_id=oidc_client_id,
            client_secret="test-secret",
            redirect_uri="https://ansina.test/callback",
            code="auth-code",
            code_verifier="verifier-value",
        )


# --- validate_id_token: AC #2's five independent rejection cases, each its own test --


def test_validate_id_token_accepts_a_fully_valid_token(
    sign_id_token: Callable[..., str],
    idp_jwks: dict[str, object],
    oidc_issuer: str,
    oidc_client_id: str,
) -> None:
    token = sign_id_token(
        sub="user-abc", nonce="the-nonce", extra_claims={"email": "a@example.com"}
    )
    jwks = jwt.PyJWKSet.from_dict(idp_jwks)

    claims = validate_id_token(
        token, jwks, issuer=oidc_issuer, audience=oidc_client_id, nonce="the-nonce"
    )

    assert claims["sub"] == "user-abc"
    assert claims["email"] == "a@example.com"


def test_validate_id_token_rejects_a_bad_signature(
    sign_id_token: Callable[..., str],
    idp_jwks: dict[str, object],
    oidc_issuer: str,
    oidc_client_id: str,
) -> None:
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = sign_id_token(nonce="n1", key=wrong_key)  # signed by a key NOT in the JWKS
    jwks = jwt.PyJWKSet.from_dict(idp_jwks)

    with pytest.raises(OidcTokenError):
        validate_id_token(
            token, jwks, issuer=oidc_issuer, audience=oidc_client_id, nonce="n1"
        )


def test_validate_id_token_rejects_a_wrong_issuer(
    sign_id_token: Callable[..., str],
    idp_jwks: dict[str, object],
    oidc_issuer: str,
    oidc_client_id: str,
) -> None:
    token = sign_id_token(nonce="n1", issuer="https://not-the-issuer.example")
    jwks = jwt.PyJWKSet.from_dict(idp_jwks)

    with pytest.raises(OidcTokenError):
        validate_id_token(
            token, jwks, issuer=oidc_issuer, audience=oidc_client_id, nonce="n1"
        )


def test_validate_id_token_rejects_a_wrong_audience(
    sign_id_token: Callable[..., str],
    idp_jwks: dict[str, object],
    oidc_issuer: str,
    oidc_client_id: str,
) -> None:
    token = sign_id_token(nonce="n1", audience="someone-elses-client-id")
    jwks = jwt.PyJWKSet.from_dict(idp_jwks)

    with pytest.raises(OidcTokenError):
        validate_id_token(
            token, jwks, issuer=oidc_issuer, audience=oidc_client_id, nonce="n1"
        )


def test_validate_id_token_rejects_an_expired_token(
    sign_id_token: Callable[..., str],
    idp_jwks: dict[str, object],
    oidc_issuer: str,
    oidc_client_id: str,
) -> None:
    token = sign_id_token(nonce="n1", exp_offset=-10.0)
    jwks = jwt.PyJWKSet.from_dict(idp_jwks)

    with pytest.raises(OidcTokenError):
        validate_id_token(
            token, jwks, issuer=oidc_issuer, audience=oidc_client_id, nonce="n1"
        )


def test_validate_id_token_rejects_a_mismatched_nonce(
    sign_id_token: Callable[..., str],
    idp_jwks: dict[str, object],
    oidc_issuer: str,
    oidc_client_id: str,
) -> None:
    token = sign_id_token(nonce="the-real-nonce")
    jwks = jwt.PyJWKSet.from_dict(idp_jwks)

    with pytest.raises(OidcTokenError, match="nonce"):
        validate_id_token(
            token,
            jwks,
            issuer=oidc_issuer,
            audience=oidc_client_id,
            nonce="a-different-nonce",
        )


def test_validate_id_token_rejects_a_missing_nonce_claim(
    sign_id_token: Callable[..., str],
    idp_jwks: dict[str, object],
    oidc_issuer: str,
    oidc_client_id: str,
) -> None:
    token = sign_id_token(omit_nonce=True)
    jwks = jwt.PyJWKSet.from_dict(idp_jwks)

    with pytest.raises(OidcTokenError, match="nonce"):
        validate_id_token(
            token, jwks, issuer=oidc_issuer, audience=oidc_client_id, nonce="n1"
        )


def test_validate_id_token_rejects_a_missing_sub_claim(
    idp_key: RSAPrivateKey,
    idp_jwks: dict[str, object],
    oidc_issuer: str,
    oidc_client_id: str,
) -> None:
    """`options={"require": [...]}` makes presence mandatory, not just value-when-
    present — an IdP that omits `sub` entirely must fail exactly as hard as one that
    sends a garbage value. Built by hand (not `sign_id_token`, which always includes
    `sub`) specifically to omit it.
    """
    now = int(time.time())
    claims = {
        "iss": oidc_issuer,
        "aud": oidc_client_id,
        "exp": now + 300,
        "iat": now,
        "nonce": "n1",
    }
    token = jwt.encode(
        claims, idp_key, algorithm="RS256", headers={"kid": "test-signing-key"}
    )
    jwks = jwt.PyJWKSet.from_dict(idp_jwks)

    with pytest.raises(OidcTokenError):
        validate_id_token(
            token, jwks, issuer=oidc_issuer, audience=oidc_client_id, nonce="n1"
        )


def test_validate_id_token_rejects_an_unrecognized_kid(
    sign_id_token: Callable[..., str],
    idp_jwks: dict[str, object],
    oidc_issuer: str,
    oidc_client_id: str,
) -> None:
    token = sign_id_token(nonce="n1", kid="a-kid-not-in-the-jwks")
    jwks = jwt.PyJWKSet.from_dict(idp_jwks)

    with pytest.raises(OidcTokenError, match="matches no key"):
        validate_id_token(
            token, jwks, issuer=oidc_issuer, audience=oidc_client_id, nonce="n1"
        )


def test_validate_id_token_selects_the_sole_key_when_no_kid_is_present(
    sign_id_token: Callable[..., str],
    idp_jwks: dict[str, object],
    oidc_issuer: str,
    oidc_client_id: str,
) -> None:
    """A token with no `kid` header still validates when the JWKS holds exactly one
    key — some IdPs omit `kid` when they only ever have one signing key.
    """
    token = sign_id_token(nonce="n1", kid=None)
    jwks = jwt.PyJWKSet.from_dict(idp_jwks)

    claims = validate_id_token(
        token, jwks, issuer=oidc_issuer, audience=oidc_client_id, nonce="n1"
    )

    assert claims["nonce"] == "n1"


def test_validate_id_token_refuses_to_guess_among_multiple_keys_with_no_kid(
    sign_id_token: Callable[..., str],
    idp_jwks: dict[str, object],
    oidc_issuer: str,
    oidc_client_id: str,
) -> None:
    second_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    second_numbers = second_key.public_key().public_numbers()

    def b64url(value: int) -> str:
        import base64

        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    existing_keys = idp_jwks["keys"]
    assert isinstance(existing_keys, list)
    two_key_jwks = {
        "keys": [
            *existing_keys,
            {
                "kty": "RSA",
                "use": "sig",
                "kid": "second-key",
                "alg": "RS256",
                "n": b64url(second_numbers.n),
                "e": b64url(second_numbers.e),
            },
        ]
    }
    token = sign_id_token(nonce="n1", kid=None)
    jwks = jwt.PyJWKSet.from_dict(two_key_jwks)

    with pytest.raises(OidcTokenError, match="matches no key"):
        validate_id_token(
            token, jwks, issuer=oidc_issuer, audience=oidc_client_id, nonce="n1"
        )


def test_validate_id_token_rejects_hs256_alg_confusion(
    idp_jwks: dict[str, object], oidc_issuer: str, oidc_client_id: str
) -> None:
    """The classic "algorithm confusion" attack: sign with HS256 using a guessed or
    derived secret, hoping a decode call that trusts the token's own `alg` header
    will verify it as a symmetric signature. `algorithms=` is pinned to
    asymmetric-only families, so this must be rejected regardless of what the forged
    header claims.
    """
    now = int(time.time())
    forged = jwt.encode(
        {
            "iss": oidc_issuer,
            "aud": oidc_client_id,
            "sub": "attacker",
            "exp": now + 300,
            "iat": now,
            "nonce": "n1",
        },
        "some-guessed-or-derived-secret-value-32bytes",
        algorithm="HS256",
        headers={"kid": "test-signing-key"},
    )
    jwks = jwt.PyJWKSet.from_dict(idp_jwks)

    with pytest.raises(OidcTokenError):
        validate_id_token(
            forged, jwks, issuer=oidc_issuer, audience=oidc_client_id, nonce="n1"
        )


def test_validate_id_token_rejects_a_malformed_token(
    idp_jwks: dict[str, object], oidc_issuer: str, oidc_client_id: str
) -> None:
    jwks = jwt.PyJWKSet.from_dict(idp_jwks)

    with pytest.raises(OidcTokenError, match="malformed"):
        validate_id_token(
            "not-a-jwt-at-all",
            jwks,
            issuer=oidc_issuer,
            audience=oidc_client_id,
            nonce="n1",
        )


# --- PKCE -----------------------------------------------------------------------------


def test_new_code_verifier_is_unique_and_url_safe() -> None:
    first = new_code_verifier()
    second = new_code_verifier()

    assert first != second
    assert 43 <= len(first) <= 128
    assert all(c.isalnum() or c in "-_" for c in first)


def test_code_challenge_matches_rfc_7636_appendix_b_vector() -> None:
    # RFC 7636 Appendix B's own published S256 test vector.
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    expected = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

    assert code_challenge(verifier) == expected


# --- HttpxOidcClient: the one real implementation, exercised against nothing (no
# network in the unit suite) — just its own error-wrapping behavior.


def test_httpx_client_wraps_connection_failure_as_provider_error() -> None:
    client: OidcHttpClient = HttpxOidcClient(timeout_seconds=0.001)

    # Port 1 on loopback is not going to accept a real connection.
    with pytest.raises(OidcProviderError):
        client.get_json("http://127.0.0.1:1/.well-known/openid-configuration")


def test_httpx_client_post_form_wraps_connection_failure_as_provider_error() -> None:
    client: OidcHttpClient = HttpxOidcClient(timeout_seconds=0.001)

    with pytest.raises(OidcProviderError):
        client.post_form("http://127.0.0.1:1/token", {"a": "b"}, auth=("id", "secret"))


def _mock_transport(status_code: int, content: bytes) -> httpx.MockTransport:
    """`httpx.MockTransport`/`BaseTransport` are part of `httpx` itself — no new test
    dependency, and no real socket needed to exercise `HttpxOidcClient`'s success and
    non-2xx paths (`FakeOidcHttpClient`, used everywhere else in this file, only
    stands in for the `OidcHttpClient` Protocol — it doesn't exercise this one real
    implementation's own `httpx.Client` plumbing).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=content)

    return httpx.MockTransport(handler)


def test_httpx_client_get_json_returns_the_parsed_body() -> None:
    client = HttpxOidcClient(
        timeout_seconds=5.0,
        transport=_mock_transport(200, b'{"issuer": "https://idp.example"}'),
    )

    result = client.get_json("http://idp.example/.well-known/openid-configuration")

    assert result == {"issuer": "https://idp.example"}


def test_httpx_client_get_json_wraps_a_non_2xx_status() -> None:
    client = HttpxOidcClient(
        timeout_seconds=5.0, transport=_mock_transport(500, b"internal error")
    )

    with pytest.raises(OidcProviderError):
        client.get_json("http://idp.example/.well-known/openid-configuration")


def test_httpx_client_post_form_returns_the_parsed_body() -> None:
    client = HttpxOidcClient(
        timeout_seconds=5.0, transport=_mock_transport(200, b'{"id_token": "abc"}')
    )

    result = client.post_form(
        "http://idp.example/token", {"a": "b"}, auth=("id", "secret")
    )

    assert result == {"id_token": "abc"}


def test_httpx_client_post_form_wraps_a_non_2xx_status() -> None:
    client = HttpxOidcClient(
        timeout_seconds=5.0, transport=_mock_transport(400, b"bad request")
    )

    with pytest.raises(OidcProviderError):
        client.post_form("http://idp.example/token", {"a": "b"}, auth=("id", "secret"))


def test_parse_json_object_rejects_invalid_json() -> None:
    response = httpx.Response(200, content=b"not json at all")

    with pytest.raises(OidcProviderError, match="did not return valid JSON"):
        _parse_json_object(response, "http://idp.example/x")


def test_parse_json_object_rejects_a_non_object_json_value() -> None:
    response = httpx.Response(200, content=b"[1, 2, 3]")

    with pytest.raises(OidcProviderError, match="isn't an object"):
        _parse_json_object(response, "http://idp.example/x")
