"""OIDC provider protocol: discovery, JWKS, token exchange, and ID-token validation.
See issue #43.

Pure protocol mechanics only — no database, no `Settings`, no provisioning. `auth.
oidc_login.OidcLoginService` is the orchestration layer that calls these functions in
order and turns their results into an Ansina user/session; this module only knows how
to talk to an IdP and prove a token it returned is genuine.

`OidcHttpClient` is the one seam this module talks to the outside world through — the
same structural-`Protocol` shape discipline `HeartRuntime`/`BrainProvider`/
`StepUpVerifier`/`Authenticator` already use elsewhere in this codebase. Tests inject a
fake implementation serving a synthetic discovery document, JWKS, and token response;
no network and no mock server needed in the unit suite. `HttpxOidcClient` is the one
real implementation, used only by `create_app`/e2e tests.

ID-token validation (`validate_id_token`) is deliberately the most heavily commented
function here: it is the one place a forged or replayed assertion could turn into an
account takeover, and issue #43's acceptance criteria require signature, issuer,
audience, expiry, and nonce to each be checked independently before anything else runs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

import httpx
import jwt

from ansina.errors import AuthError

if TYPE_CHECKING:
    from collections.abc import Mapping


class OidcProviderError(AuthError):
    """The IdP (or the network path to it) failed to hold up its end: unreachable,
    a non-2xx response, malformed JSON, a discovery document naming a different
    issuer than the one configured, a discovery document missing a required
    endpoint, or a JWKS document `PyJWT` can't parse. Never leaks the underlying
    transport exception's type to a caller — just its message, for diagnosis.
    """

    code: ClassVar[str] = "ansina.auth.oidc_provider_unavailable"


class OidcTokenError(AuthError):
    """The ID token itself failed validation — bad signature, wrong `iss`/`aud`,
    expired/not-yet-valid `exp`/`nbf`, a missing required claim, an unrecognized
    `kid`, or a `nonce` that doesn't match the one this login issued. Every one of
    issue #43's AC #2 rejection cases raises this, and only this — a caller can't
    distinguish "wrong signature" from "wrong nonce" by exception type, deliberately
    (the same "no oracle" discipline `BearerAuthMiddleware`'s 401 already follows).
    """

    code: ClassVar[str] = "ansina.auth.oidc_token_invalid"


# Asymmetric algorithms only — RS/PS/ES across the three NIST-recommended digest
# sizes. Never `none` (PyJWT already refuses it unless explicitly allowed) and never
# an `HS*` (symmetric) family: if it were allowed, a JWKS's public RSA/EC key could be
# fed back to `jwt.decode` as an HMAC *secret*, letting anyone who can compute
# HMAC-SHA256 over a known public key forge a token — the classic "algorithm
# confusion" attack on a decode call that trusts the token's own `alg` header. Pinning
# this list, rather than trusting the header, is what closes that off.
_ALLOWED_ALGORITHMS: tuple[str, ...] = (
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
)

_REQUIRED_DISCOVERY_FIELDS: tuple[str, ...] = (
    "authorization_endpoint",
    "token_endpoint",
    "jwks_uri",
)

# RFC 7636 recommends a code_verifier of 43-128 characters after base64url encoding;
# 64 raw bytes encodes to 86, comfortably inside that range — the same generation
# shape (`secrets.token_urlsafe`) `auth.bootstrap`'s bootstrap token and `api.tokens`'
# minted tokens already use.
_CODE_VERIFIER_BYTES = 64


class OidcHttpClient(Protocol):
    """Everything this module needs from an HTTP client, and nothing more — a fake
    implementation in tests never needs to speak real HTTP.
    """

    def get_json(self, url: str) -> dict[str, Any]: ...

    def post_form(
        self, url: str, form: Mapping[str, str], *, auth: tuple[str, str]
    ) -> dict[str, Any]: ...


def _parse_json_object(response: httpx.Response, url: str) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:  # json.JSONDecodeError subclasses ValueError
        raise OidcProviderError(f"{url} did not return valid JSON") from exc
    if not isinstance(data, dict):
        raise OidcProviderError(f"{url} returned a JSON value that isn't an object")
    return data


class HttpxOidcClient:
    """The real `OidcHttpClient`: a fresh, short-lived `httpx.Client` per call.

    Federated login is an infrequent, human-paced operation — not a per-request hot
    path like `ApiTokenAuthenticator` — so there's no persistent connection worth
    keeping open across calls, and consequently no `aclose()` lifecycle to wire into
    `create_app`'s shutdown the way `BrainProvider`'s long-lived client needs.

    `transport` is `None` in production (the real network transport) and an
    `httpx.MockTransport` in this module's own unit tests — `httpx.MockTransport`/
    `BaseTransport` are part of `httpx` itself, so this needs no new test dependency
    and no real socket to exercise a non-2xx response or a malformed body, unlike the
    `OidcHttpClient` Protocol's fakeable shape (`FakeOidcHttpClient`), which this
    class is the one implementation *not* covered by.
    """

    def __init__(
        self, *, timeout_seconds: float, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def get_json(self, url: str) -> dict[str, Any]:
        try:
            with httpx.Client(
                timeout=self._timeout_seconds, transport=self._transport
            ) as client:
                response = client.get(url)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OidcProviderError(f"request to {url} failed: {exc}") from exc
        return _parse_json_object(response, url)

    def post_form(
        self, url: str, form: Mapping[str, str], *, auth: tuple[str, str]
    ) -> dict[str, Any]:
        try:
            with httpx.Client(
                timeout=self._timeout_seconds, transport=self._transport
            ) as client:
                response = client.post(url, data=dict(form), auth=auth)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OidcProviderError(f"request to {url} failed: {exc}") from exc
        return _parse_json_object(response, url)


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    """The subset of an OIDC discovery document this module actually uses."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


def discover(client: OidcHttpClient, issuer: str) -> ProviderMetadata:
    """`GET {issuer}/.well-known/openid-configuration` (RFC 8414 / OIDC Discovery
    1.0). Fetched fresh on every call — no caching layer, deliberately: a login is
    infrequent enough that the extra round trip is unnoticeable, and it means an IdP
    that rotates its endpoints or keys is honored on the very next login rather than
    however long a cache TTL happened to be.

    Refuses (`OidcProviderError`) if the document's own `issuer` field doesn't match
    the configured `issuer` exactly (RFC 8414 §3.3's own validation requirement — a
    discovery document is only trustworthy for the issuer it claims to be), or if any
    of `authorization_endpoint`/`token_endpoint`/`jwks_uri` is missing.
    """
    # Normalized once, consistently, on both sides of the comparison below — a
    # trailing slash on the *caller's* `issuer` argument (already stripped at the
    # config layer by `OidcSettings._strip_trailing_slash` for the normal path, but
    # this function has direct callers of its own too, e.g. tests) must never turn
    # into a spurious mismatch against a well-formed IdP's own slash-free `issuer`.
    normalized_issuer = issuer.rstrip("/")
    url = f"{normalized_issuer}/.well-known/openid-configuration"
    document = client.get_json(url)

    returned_issuer = document.get("issuer")
    if returned_issuer != normalized_issuer:
        raise OidcProviderError(
            f"{url} advertises issuer {returned_issuer!r}, but the configured issuer "
            f"is {normalized_issuer!r} — refusing to trust a discovery document for "
            "a different issuer than the one configured"
        )

    missing = [name for name in _REQUIRED_DISCOVERY_FIELDS if not document.get(name)]
    if missing:
        raise OidcProviderError(
            f"{url} is missing required field(s): {', '.join(missing)}"
        )

    return ProviderMetadata(
        issuer=normalized_issuer,
        authorization_endpoint=document["authorization_endpoint"],
        token_endpoint=document["token_endpoint"],
        jwks_uri=document["jwks_uri"],
    )


def fetch_jwks(client: OidcHttpClient, jwks_uri: str) -> jwt.PyJWKSet:
    """`GET jwks_uri` and parse it as a JWK Set (RFC 7517). Fetched fresh on every
    login, same reasoning as `discover` above — an IdP's key rotation is honored on
    the very next login.
    """
    document = client.get_json(jwks_uri)
    try:
        return jwt.PyJWKSet.from_dict(document)
    # `PyJWKSetError` (e.g. no "keys" array, or an empty one) is a sibling of
    # `PyJWKError` under `PyJWTError`, not a subclass of it — catching only
    # `PyJWKError` would let a malformed-but-not-individually-invalid-key JWKS
    # document escape as a bare, unmapped exception instead of `OidcProviderError`.
    except (jwt.PyJWKError, jwt.PyJWKSetError) as exc:
        raise OidcProviderError(f"{jwks_uri} returned an invalid JWKS: {exc}") from exc


def exchange_code(
    client: OidcHttpClient,
    metadata: ProviderMetadata,
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
) -> str:
    """Redeem an authorization `code` at the token endpoint (RFC 6749 §4.1.3), client
    authentication via HTTP Basic, PKCE `code_verifier` alongside it (RFC 7636 §4.5)
    so a party that only intercepted the authorization code — but never saw the
    `code_verifier` this login's own `oidc_login_states` row held — can't redeem it.

    Returns the raw `id_token` string. Raises `OidcTokenError` if the token response
    carries no usable `id_token` — a response with only an `access_token` (an IdP not
    actually running the OIDC profile, or a `scope` that never included `openid`) is
    not something this login exchange can use.
    """
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    response = client.post_form(
        metadata.token_endpoint, form, auth=(client_id, client_secret)
    )
    id_token = response.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        raise OidcTokenError(
            "the token endpoint response carried no id_token — the identity "
            "provider may not be OIDC-compliant, or the requested scopes didn't "
            "include 'openid'"
        )
    return id_token


def _select_signing_key(jwks: jwt.PyJWKSet, kid: str | None) -> jwt.PyJWK | None:
    """The JWKS entry matching `kid`, or — only when the token carries no `kid` at
    all and the JWKS holds exactly one key — that sole key. Never guesses among
    multiple candidates: an ambiguous match is treated as no match.
    """
    if kid is not None:
        for key in jwks.keys:
            if key.key_id == kid:
                return key
        return None
    if len(jwks.keys) == 1:
        return jwks.keys[0]
    return None


def validate_id_token(
    id_token: str,
    jwks: jwt.PyJWKSet,
    *,
    issuer: str,
    audience: str,
    nonce: str,
) -> dict[str, Any]:
    """Fully verify `id_token` and return its claims. Every one of issue #43's AC #2
    cases — bad signature, wrong issuer, wrong audience, expired `exp`, mismatched
    nonce — is checked here, independently, before any provisioning or role-mapping
    call runs: this function touches nothing but the token and the JWKS it's handed.

    Signature verification selects the key by the token's own `kid` header
    (`_select_signing_key`) rather than trying every JWKS entry — an unrecognized
    `kid` is a hard failure, not a fallback to "try them all." `algorithms` is pinned
    to `_ALLOWED_ALGORITHMS` (see its own comment) rather than trusting the token's
    `alg` header. `options={"require": [...]}` makes every listed claim's *presence*
    mandatory, not just its value when present — an IdP that omits `exp` entirely
    must fail exactly as hard as one that sends an already-expired one.

    `nonce` has no concept in `jwt.decode` itself (it's an OIDC-layer claim, not a
    JWT-layer one), so it's checked separately here with `hmac.compare_digest` — the
    same constant-time-comparison discipline `auth.hashing.verify_token_hash` already
    uses for token comparisons — against the value `oidc_login_states` held for this
    login, proving *this* login produced *this* token rather than a replayed one.
    """
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise OidcTokenError(f"malformed id_token: {exc}") from exc

    kid = header.get("kid")
    key = _select_signing_key(jwks, kid if isinstance(kid, str) else None)
    if key is None:
        raise OidcTokenError(
            f"id_token's kid {kid!r} matches no key in the issuer's JWKS"
        )

    try:
        claims: dict[str, Any] = jwt.decode(
            id_token,
            key=key,
            algorithms=list(_ALLOWED_ALGORITHMS),
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise OidcTokenError(f"id_token failed validation: {exc}") from exc

    token_nonce = claims.get("nonce")
    if not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce, nonce):
        raise OidcTokenError("id_token's nonce does not match this login's own")

    return claims


def new_code_verifier() -> str:
    """A fresh PKCE `code_verifier` (RFC 7636 §4.1) — high-entropy, url-safe."""
    return secrets.token_urlsafe(_CODE_VERIFIER_BYTES)


def code_challenge(code_verifier: str) -> str:
    """The PKCE S256 `code_challenge` for `code_verifier` (RFC 7636 §4.2):
    base64url(sha256(code_verifier)), unpadded.
    """
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
