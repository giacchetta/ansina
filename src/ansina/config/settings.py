"""Layered, fail-fast configuration for Ansina.

Precedence, lowest to highest: built-in defaults -> ``ansina.toml`` -> ``ANSINA_*``
environment variables. Every subsystem gets its config from a loaded :class:`Settings`
instance — nothing in this codebase calls ``os.getenv`` directly.

Secrets (tokens, API keys) are read from environment variables only. A secret-typed
field (``SecretStr``) set in ``ansina.toml`` is a hard configuration error, not a
silently accepted value — see ``.agents/guardrails/secret-prevention.md``.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import math
import os
import re
import tomllib
from collections import Counter
from collections.abc import Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)
from pydantic_settings.exceptions import SettingsError

from ansina.errors import ConfigurationError

_ENV_PREFIX = "ANSINA_"
_DEFAULT_CONFIG_FILE = Path("ansina.toml")
_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)


class ConfigError(ConfigurationError):
    """Raised when configuration fails to load, with one aggregated, readable report."""

    code = "ansina.config.invalid"


class ServerSettings(BaseModel):
    """Where the REST API binds. Defaults to loopback-only, per issue #5."""

    model_config = _MODEL_CONFIG

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)


class LoggingSettings(BaseModel):
    """Log verbosity, consumed by issue #3's structured logging setup."""

    model_config = _MODEL_CONFIG

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class DatabaseSettings(BaseModel):
    """SQLite location, consumed by issue #6's persistence foundation."""

    # `validate_default=True` (unlike the shared `_MODEL_CONFIG`) so the *default*
    # path goes through `_resolve_path` too, not just an explicitly configured one —
    # otherwise the unconfigured case would stay relative to whatever the process's
    # CWD is, the exact ambiguity this validator exists to remove.
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    path: Path = Path("ansina.db")

    @field_validator("path")
    @classmethod
    def _resolve_path(cls, value: Path) -> Path:
        """Expand `~` and anchor a relative path to the CWD, once, at load time.

        Without this, `~/data/ansina.db` stays literal (SQLite would create a
        directory named `~`) and a relative path silently tracks wherever the
        process happens to be launched from — fine from the repo root, wrong the
        moment Ansina runs as a service. Every consumer downstream sees one
        already-absolute path.
        """
        return value.expanduser().resolve()


class TickSettings(BaseModel):
    """The autonomic tick loop's cadence, consumed by issue #11's `ansina.heart.tick`.

    Nested under `[heart]` rather than top-level: the tick loop only ever exists
    alongside a loaded Heart, so `[heart] enabled = false` (still the default) already
    gates it — there is no independent "tick loop without a Heart" configuration.
    """

    model_config = _MODEL_CONFIG

    enabled: bool = True
    interval_seconds: float = Field(default=30.0, gt=0)
    # Uniform random delay added to when the loop wakes for a scheduled tick, so a
    # freshly restarted process doesn't tick in lockstep with anything else on a fixed
    # cadence — thundering-herd-style alignment, not overlap (see `TickLoop`'s own
    # backpressure guard for that).
    jitter_seconds: float = Field(default=3.0, ge=0)


class HeartSettings(BaseModel):
    """The in-process Heart runtime, consumed by issue #10's `ansina.heart`.

    `enabled=False` (the default) means no capability probe runs, no model loads, and
    `/readyz` carries no `heart` key at all — `uv run ansina`, the E2E suite, and CI
    are unaffected until this is turned on. `runtime` is `Literal["auto", "mlx"]`
    only: MLX is the sole adapter this milestone ships (see issue #10's PR
    description for why the llama-cpp-python fallback was deferred), so `"auto"` and
    `"mlx"` currently behave identically — the enum exists so a future adapter can
    add a member without a config break, not to advertise one that doesn't exist yet.
    """

    # `validate_default=True` for the same reason as `DatabaseSettings`: the default
    # `cache_dir` must go through `_resolve_paths` too, not just an explicit one.
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    enabled: bool = False
    runtime: Literal["auto", "mlx"] = "auto"
    model_path: Path | None = None
    model_repo: str = "mlx-community/Qwen3-4B-Instruct-2507-4bit"
    cache_dir: Path = Path("~/.cache/ansina/models")
    # The blueprint's 8k context budget is a hard ceiling, not a target (issue #10) —
    # enforced here so it can never be configured past what the Heart's prompts are
    # allowed to assume.
    context_tokens: int = Field(default=8192, ge=256, le=8192)
    max_output_tokens: int = Field(default=512, ge=1)
    tick: TickSettings = Field(default_factory=TickSettings)

    @field_validator("model_path", "cache_dir")
    @classmethod
    def _resolve_paths(cls, value: Path | None) -> Path | None:
        """Same `~`-expand-and-anchor-to-CWD treatment as
        `DatabaseSettings._resolve_path` — a relative `cache_dir` or `model_path`
        must not silently track wherever the process happens to be launched from.
        """
        if value is None:
            return None
        return value.expanduser().resolve()


class BrainSettings(BaseModel):
    """The remote Brain provider, consumed by issue #12's `ansina.brain`.

    `enabled=False` (the default) means `build_brain_provider` is never called and
    `app.state.brain` stays `None` — same shape as `HeartSettings.enabled`. `api_key`
    is env-only (see module docstring); a keyless `base_url` still pointing at the
    default OpenAI host is refused at selection time (`ansina.brain.selection`) — a
    keyless *custom* `base_url` (a local OpenAI-compatible server) is legitimate and
    stays allowed.
    """

    model_config = _MODEL_CONFIG

    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key: SecretStr | None = Field(default=None, min_length=16)
    timeout_seconds: float = Field(default=60.0, gt=0)
    max_output_tokens: int = Field(default=2048, ge=1)
    # Bounded retry (issue #12): `max_retries=0` disables retry entirely rather than
    # meaning "unbounded" — there is no unbounded option.
    max_retries: int = Field(default=3, ge=0)
    retry_initial_backoff_seconds: float = Field(default=1.0, gt=0)
    retry_max_backoff_seconds: float = Field(default=30.0, gt=0)
    # Optional: with no price configured (the default), `BrainUsage.cost_usd` stays
    # `None` rather than reporting a fabricated figure.
    price_per_1m_input_tokens: float | None = Field(default=None, ge=0)
    price_per_1m_output_tokens: float | None = Field(default=None, ge=0)


class PasswordHashSettings(BaseModel):
    """Everything about passwords: argon2id work factors (issue #24) plus the
    acceptability policy `ansina.auth.password_policy.assert_password_acceptable`
    enforces at every password-setting path (issue #48). One table, kept under its
    original name — widening it here needed no config-key rename and breaks no
    existing `ansina.toml`.

    `time_cost`/`memory_cost_kib`/`parallelism` follow the OWASP-recommended argon2id
    baseline (m=64 MiB, t=3, p=4) — high enough to make an offline brute-force of a
    stolen hash expensive, low enough not to dominate a login request. The unit suite
    overrides these with minimal values (see `tests/unit/auth/conftest.py`) so hashing
    doesn't dominate test runtime.

    `min_length` defaults to 12 but is bounded `ge=8` — configurable down, but never
    below NIST SP 800-63B's own stated minimum for a user-chosen secret, the same
    standard `password_policy`'s module docstring cites for deliberately *not* adding
    character-class/composition rules. `max_length` (default 1024) is an argon2
    work-factor DoS ceiling, not a strength rule — it bounds how large an input a
    caller can force an expensive hash over, nothing more. `reject_common` toggles the
    bundled common-password list (`auth/data/common_passwords.txt`).
    """

    model_config = _MODEL_CONFIG

    time_cost: int = Field(default=3, ge=1)
    memory_cost_kib: int = Field(default=65536, ge=1)
    parallelism: int = Field(default=4, ge=1)

    min_length: int = Field(default=12, ge=8)
    max_length: int = Field(default=1024, ge=8)
    reject_common: bool = True

    @model_validator(mode="after")
    def _validate_length_bounds(self) -> PasswordHashSettings:
        """A config where no password could ever be accepted must fail at boot, not
        surface as every password-setting request mysteriously refusing everything —
        the same "fail loudly before uvicorn binds a port" reasoning
        `OidcSettings._validate_enabled_requires_credentials` already applies.
        """
        if self.max_length < self.min_length:
            raise ConfigError(
                _render_report(
                    [
                        "security.password.max_length must be >= "
                        "security.password.min_length "
                        f"(got max_length={self.max_length}, "
                        f"min_length={self.min_length})"
                    ]
                )
            )
        return self


class SudoSettings(BaseModel):
    """Sudo step-up tuning for `ansina.auth.sudo.SudoService`, consumed by issue #26.

    Defaults follow the issue's own stated numbers: a 10-minute grant, locked out
    after 5 consecutive failures within a 5-minute window, for 15 minutes.
    """

    model_config = _MODEL_CONFIG

    ttl_seconds: float = Field(default=600.0, gt=0)
    max_failed_attempts: int = Field(default=5, ge=1)
    attempt_window_seconds: float = Field(default=300.0, gt=0)
    lockout_seconds: float = Field(default=900.0, gt=0)


# AES-256 needs exactly 32 raw bytes; `secrets.token_urlsafe(32)` is the generator this
# validator's own error message recommends, so the shape it produces (unpadded
# url-safe base64) is exactly what's accepted here.
_ENCRYPTION_KEY_BYTES = 32


class EncryptionSettings(BaseModel):
    """AES-GCM key for TOTP secrets at rest (`ansina.auth.encryption`, issue #41).

    Env-only, the same "SecretStr in TOML is a startup error" rule every other secret
    already follows — no new exemption. Unset by default: nothing before issue #41
    ever writes an encrypted-at-rest secret, so requiring this key unconditionally at
    every boot would break every deployment that never enrolls a TOTP user.
    `ansina.auth.encryption.ensure_key_configured_if_needed` is what actually enforces
    "unset + a totp credential already exists" as a boot refusal — this model has no
    way to see whether any `credentials` row exists, so that check can't live here.
    """

    model_config = _MODEL_CONFIG

    key: SecretStr | None = Field(default=None)

    @field_validator("key")
    @classmethod
    def _validate_key_shape(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return value
        raw = value.get_secret_value()
        try:
            decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                "security.encryption.key must be url-safe base64 (the same shape "
                "secrets.token_urlsafe(32) produces)"
            ) from exc
        if len(decoded) != _ENCRYPTION_KEY_BYTES:
            raise ValueError(
                "security.encryption.key must decode to exactly "
                f"{_ENCRYPTION_KEY_BYTES} bytes (AES-256), got {len(decoded)} — "
                "generate one with e.g. "
                '`python -c "import secrets; print(secrets.token_urlsafe(32))"`'
            )
        return value


# The bootstrap identity's own username (`ansina.auth.bootstrap`'s synthetic Admin) —
# defined here, not there, so `SecuritySettings.admin_username`'s reserved-name check
# can reference it without `config` gaining a dependency on `auth` (the reverse
# direction already exists pervasively; `config` itself imports nothing from `auth`
# anywhere in this codebase, and this one shared string shouldn't be the first).
# `auth.bootstrap` imports this constant rather than declaring its own copy.
RESERVED_BOOTSTRAP_USERNAME = "bootstrap-admin"

_TOKEN_MIN_LENGTH = 32
# The alphabet `secrets.token_urlsafe()`/`token_hex()` draw from — restricting a
# manually-supplied token to it rejects any human-typed phrase (spaces, punctuation,
# mixed scripts) outright, before entropy is even considered.
_TOKEN_CHARSET = re.compile(r"^[A-Za-z0-9_-]+$")
# Best-effort deterrent, not a cryptographic guarantee: a sufficiently-crafted
# adversarial string can still clear this bar. Calibrated against `secrets.token_hex()`
# — the lowest-entropy-per-char generator Ansina recommends — whose observed floor
# across 2000 trials at this length is ~3.0 bits/char; 2.5 leaves margin against a
# false rejection of a genuinely random token while still catching padded/repeated
# strings (e.g. a word repeated to hit the length floor lands around 2.9, an all-one-
# character string at 0.0).
_TOKEN_MIN_ENTROPY_BITS_PER_CHAR = 2.5


def _token_entropy_bits_per_char(value: str) -> float:
    """Shannon entropy of `value`'s own character distribution, in bits/char."""
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum((n / length) * math.log2(n / length) for n in counts.values())


class OidcSettings(BaseModel):
    """OAuth 2.0 / OIDC federated login exchange settings, consumed by issue #43's
    `ansina.auth.oidc`/`ansina.auth.oidc_login` and served over `POST /auth/oidc/login`
    + `GET /auth/oidc/callback`.

    `enabled=False` (the default) means `build_oidc_login_service` returns `None` and
    `app.state.oidc` stays `None` — same shape as `HeartSettings.enabled`/
    `BrainSettings.enabled` — and the two OIDC routes answer 503 rather than attempting
    a code flow against nothing. Unlike Heart/Brain, enabling this performs no boot-time
    network call: an unreachable or misconfigured IdP must never stop Ansina from
    booting, only fail the individual login attempt.

    `client_secret` is env-only, the same "`SecretStr` in TOML is a startup error" rule
    every other secret already follows — no new exemption; `_AnsinaTomlSource` already
    enforces this generically via `_walk_secret_paths`, so no new code is needed for it.
    """

    model_config = _MODEL_CONFIG

    enabled: bool = False
    issuer: str = ""
    client_id: str = ""
    client_secret: SecretStr | None = None
    redirect_uri: str = ""
    scopes: tuple[str, ...] = ("openid", "profile", "email")

    @field_validator("issuer")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        """A real IdP's discovery document publishes its own `issuer` without a
        trailing slash (and an ID token's `iss` claim must match that exactly, per
        the OIDC spec's strict string-identity comparison) — so an operator who
        copy-pastes an issuer URL *with* one would otherwise fail discovery and token
        validation alike, unconditionally, against an otherwise perfectly healthy
        IdP. Normalized once, here, the same "clean at load time" treatment
        `DatabaseSettings._resolve_path`/`HeartSettings._resolve_paths` already give
        a config value with an analogous footgun.
        """
        return value.rstrip("/")

    # How long the Ansina `api_token` minted at the end of a successful login stays
    # valid (issue #39's `ttl_seconds` seam, issue #43's first HTTP-reachable caller of
    # it) — mirrors `SudoSettings.ttl_seconds`'s own per-feature TTL knob rather than
    # reusing an unrelated one.
    token_ttl_seconds: float = Field(default=3600.0, gt=0)
    # How long an issued `state`/`nonce`/PKCE `code_verifier` (`oidc_login_states`) may
    # sit unconsumed before `OidcLoginStateRepository.take` treats it as expired —
    # bounds how long a browser may sit on the IdP's login page before the flow must be
    # restarted.
    state_ttl_seconds: float = Field(default=600.0, gt=0)
    # Timeout for every outbound call to the IdP (discovery, JWKS, token exchange) —
    # `ansina.auth.oidc.HttpxOidcClient`'s own `httpx.Client(timeout=...)`.
    http_timeout_seconds: float = Field(default=10.0, gt=0)

    @model_validator(mode="after")
    def _validate_enabled_requires_credentials(self) -> OidcSettings:
        """`enabled=True` with any of `issuer`/`client_id`/`client_secret`/
        `redirect_uri` left unset can never actually authenticate anyone — refused as
        a boot-time `ConfigError`, the same "fail loudly, don't discover this at the
        first login attempt" reasoning
        `SecuritySettings._validate_admin_username_pairing` already applies. Raised
        directly (not `ValueError`) since this is a model-level check with no single
        field `loc` for `_format_validation_error` to key on — the same pattern that
        pairing check and `Settings._refuse_unsafe_bind` both use.
        """
        if not self.enabled:
            return self
        missing = [
            name
            for name, value in (
                ("issuer", self.issuer),
                ("client_id", self.client_id),
                ("redirect_uri", self.redirect_uri),
            )
            if not value
        ]
        if self.client_secret is None:
            missing.append("client_secret")
        if missing:
            fields = ", ".join(f"security.oidc.{name}" for name in missing)
            raise ConfigError(
                _render_report(
                    [
                        f"{fields}: required when security.oidc.enabled = true "
                        "(set ANSINA_SECURITY__OIDC__ISSUER, "
                        "ANSINA_SECURITY__OIDC__CLIENT_ID, "
                        "ANSINA_SECURITY__OIDC__CLIENT_SECRET, and "
                        "ANSINA_SECURITY__OIDC__REDIRECT_URI, or leave "
                        "security.oidc.enabled = false)"
                    ]
                )
            )
        return self


class SecuritySettings(BaseModel):
    """Auth material for issue #5, #24, and #28.

    `enabled=False` is the only way to run with no authentication at all (loopback-only
    — see `Settings._refuse_unsafe_bind`) — a deliberate, explicit opt-out. When
    `enabled=True` (the default), Ansina *always* generates its own high-entropy
    bootstrap token on first boot and prints it once (`ansina.auth.bootstrap`) —
    nobody, including Ansina's own logs, ever sees it again, and nothing in this file
    can override or rotate it. `api_token` below is unrelated to that identity — see
    its own docstring.
    """

    model_config = _MODEL_CONFIG

    enabled: bool = True

    # Issue #28: together with `api_token`, provisions a second, *ordinary* Admin user
    # once, at first boot — the "configured admin" (`ansina.auth.bootstrap.
    # ensure_configured_admin`), distinct from the synthetic bootstrap identity above.
    # It exists so a scripted install or CI gets a known, reproducible Admin credential
    # without ever touching the bootstrap identity's own credential or scraping the
    # once-only banner off stdout. Required together with `api_token` — one set without
    # the other is a config error (`_validate_admin_username_pairing` below). Never the
    # reserved bootstrap username. Created once; on any later boot where a non-bootstrap
    # user already exists, both fields are silently ignored (one info log line, not a
    # boot failure) — a systemd unit or container keeping the same environment across
    # every restart must not crash on a routine one. There is no rotation: once created,
    # this identity is an ordinary user and mints/revokes its own tokens the same way
    # any other user does, via `POST`/`DELETE /auth/me/tokens`.
    admin_username: str | None = Field(default=None, min_length=1)

    # Issue #28: the configured admin's own credential — see `admin_username` above.
    # No literal default — ever. Validated to look like a securely generated value
    # (length, charset, entropy) rather than a human-chosen phrase; see
    # `_validate_token_strength` below. This field no longer has anything to do with
    # the bootstrap identity, which #28 made permanently non-overridable.
    api_token: SecretStr | None = Field(default=None, min_length=_TOKEN_MIN_LENGTH)

    # Issue #24: on first boot with no users, this resolves to a single synthetic Admin
    # identity (`ansina.auth.bootstrap`) so the service stays reachable. Setting this
    # `False` revokes that identity's credential (but keeps the user and its
    # `external_identities` row, for audit-log attribution) once a real Admin account
    # exists — it does not prevent the bootstrap identity from ever being created, and
    # setting it back to `True` regenerates a credential if it's currently
    # credential-less (issue #28 — this is now the only break-glass path, so it must
    # be recoverable), but never touches one that's still live.
    bootstrap_admin_enabled: bool = True
    password: PasswordHashSettings = Field(default_factory=PasswordHashSettings)
    sudo: SudoSettings = Field(default_factory=SudoSettings)
    encryption: EncryptionSettings = Field(default_factory=EncryptionSettings)
    oidc: OidcSettings = Field(default_factory=OidcSettings)

    # Issue #28: how long a token's `last_used_at` may go stale before the next
    # successful authentication updates it — coalesced rather than written on every
    # request, since `find_user_by_api_token`/`find_api_token_credential` already
    # full-scan `credentials` on every authenticated request, and a write there would
    # serialize SQLite writers against the tick loop and any concurrent reader. `0`
    # means "write on every authentication" — a useful escape hatch, not the default.
    token_last_used_resolution_seconds: float = Field(default=300.0, ge=0)

    @field_validator("admin_username")
    @classmethod
    def _validate_admin_username_not_reserved(cls, value: str | None) -> str | None:
        if value == RESERVED_BOOTSTRAP_USERNAME:
            raise ValueError(
                f"{RESERVED_BOOTSTRAP_USERNAME!r} is a reserved user, choose "
                "another one"
            )
        return value

    @model_validator(mode="after")
    def _validate_admin_username_pairing(self) -> SecuritySettings:
        """`admin_username` and `api_token` provision the configured admin together —
        neither means anything alone: a token with no username has nothing to name, a
        username with no token has nothing to authenticate. Raised as `ConfigError`
        directly, not `ValueError` — same reasoning as `Settings._refuse_unsafe_bind`:
        a model-level check like this has no single field `loc` for pydantic's
        aggregated-error formatting to key on.
        """
        if (self.admin_username is None) != (self.api_token is None):
            raise ConfigError(
                _render_report(
                    [
                        "security.admin_username / security.api_token: both must be "
                        "set together to provision the configured admin identity, or "
                        "neither — set ANSINA_SECURITY__ADMIN_USERNAME and "
                        "ANSINA_SECURITY__API_TOKEN together, or leave both unset"
                    ]
                )
            )
        return self

    @field_validator("api_token")
    @classmethod
    def _validate_token_strength(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return value
        raw = value.get_secret_value()
        if not _TOKEN_CHARSET.match(raw):
            raise ValueError(
                "api_token must contain only letters, digits, '-' and '_' (the same "
                "alphabet a securely generated token uses) — generate one with e.g. "
                '`python -c "import secrets; print(secrets.token_urlsafe(32))"`'
            )
        entropy = _token_entropy_bits_per_char(raw)
        if entropy < _TOKEN_MIN_ENTROPY_BITS_PER_CHAR:
            raise ValueError(
                f"api_token looks too predictable ({entropy:.2f} bits/char of its own "
                f"character distribution, need >= {_TOKEN_MIN_ENTROPY_BITS_PER_CHAR}) "
                "— use a securely generated random token, not a human-chosen or "
                "padded/repeated one"
            )
        return value


# Holds an explicit `load_settings(config_file=...)` override for the duration of that
# call. `Settings.settings_customise_sources` is a classmethod with a fixed signature
# (pydantic-settings calls it with no extra arguments), so this is how the override
# reaches it without a module-level mutable global.
_config_file_override: ContextVar[Path | None] = ContextVar(
    "_config_file_override", default=None
)


def _resolve_config_file() -> Path:
    """Explicit argument > ``ANSINA_CONFIG_FILE`` env var > ``./ansina.toml``."""
    override = _config_file_override.get()
    if override is not None:
        return override
    env_path = os.environ.get("ANSINA_CONFIG_FILE")
    if env_path:
        return Path(env_path)
    return _DEFAULT_CONFIG_FILE


# These two walk arbitrary pydantic field annotations (str, SecretStr, Optional[...],
# nested BaseModel subclasses, ...) to build the secret-field map below, so `Any` here
# is the type being inspected, not a laziness shortcut.


def _is_secret_annotation(annotation: Any) -> bool:  # noqa: ANN401
    if annotation is SecretStr:
        return True
    args = get_args(annotation)
    return any(_is_secret_annotation(arg) for arg in args)


def _unwrap_model(annotation: Any) -> type[BaseModel] | None:  # noqa: ANN401
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in get_args(annotation):
        nested = _unwrap_model(arg)
        if nested is not None:
            return nested
    return None


def _walk_secret_paths(
    model: type[BaseModel], prefix: tuple[str, ...] = ()
) -> set[tuple[str, ...]]:
    """Every field path in `model` whose type is (optionally) `SecretStr`.

    Derived from the model tree rather than hardcoded, so a secret field added by a
    later milestone is covered automatically.
    """
    paths: set[tuple[str, ...]] = set()
    for name, field in model.model_fields.items():
        path = (*prefix, name)
        if _is_secret_annotation(field.annotation):
            paths.add(path)
            continue
        nested = _unwrap_model(field.annotation)
        if nested is not None:
            paths |= _walk_secret_paths(nested, path)
    return paths


def _find_secret_key_paths(
    data: dict[str, Any],
    secret_paths: set[tuple[str, ...]],
    prefix: tuple[str, ...] = (),
) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    for key, value in data.items():
        path = (*prefix, key)
        if path in secret_paths:
            found.append(path)
        elif isinstance(value, dict):
            found.extend(_find_secret_key_paths(value, secret_paths, path))
    return found


def _format_loc(loc: Sequence[str]) -> str:
    return ".".join(loc)


def _env_var_name(loc: Sequence[str]) -> str:
    return "ANSINA_" + "__".join(part.upper() for part in loc)


def _toml_location(loc: Sequence[str]) -> str:
    if len(loc) == 1:
        return f'top-level key "{loc[0]}"'
    *table, key = loc
    return f"[{'.'.join(table)}] {key}"


def _is_loopback(host: str) -> bool:
    """Fail-closed: `True` only for a host that is unambiguously loopback-only.

    `"localhost"` and any address `ipaddress` parses as loopback (`127.0.0.1`,
    `127.0.0.53`, `::1`, ...) are loopback. Anything unparseable, and the
    all-interfaces spellings (`""`, `"0.0.0.0"`, `"::"`), are treated as *not*
    loopback — issue #5 wants a false negative here (an unnecessary startup refusal)
    rather than a false positive (a network-exposed server that skipped the check).
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _render_report(problems: Sequence[str]) -> str:
    count = len(problems)
    noun = "problem" if count == 1 else "problems"
    lines = [f"Invalid Ansina configuration — {count} {noun}"]
    lines.extend(f"  - {problem}" for problem in problems)
    return "\n".join(lines)


class _AnsinaTomlSource(TomlConfigSettingsSource):
    """`TomlConfigSettingsSource` plus a hard refusal of secret-typed keys.

    Secrets must come from the environment (see module docstring); finding one in the
    file is a configuration error, reported the same way any other bad field is.
    """

    def __init__(self, settings_cls: type[BaseSettings], toml_file: Path) -> None:
        super().__init__(settings_cls, toml_file=toml_file)
        secret_paths = _walk_secret_paths(settings_cls)
        found = _find_secret_key_paths(self.toml_data, secret_paths)
        if found:
            problems = [
                f"{_format_loc(path)}: secrets must be set via the "
                f"{_env_var_name(path)} environment variable, not {toml_file} "
                f"(found {_toml_location(path)})"
                for path in found
            ]
            raise ConfigError(_render_report(problems))


class Settings(BaseSettings):
    """The single typed configuration object every subsystem loads from.

    Construct via :func:`load_settings`, not directly — the layering (TOML file,
    env-only secrets, aggregated errors) is wired through that function's use of
    `settings_customise_sources` below, not through calling `Settings()` on its own
    from arbitrary code.
    """

    model_config = SettingsConfigDict(
        env_prefix=_ENV_PREFIX,
        env_nested_delimiter="__",
        extra="forbid",
        frozen=True,
        validate_default=True,
    )

    server: ServerSettings = Field(default_factory=ServerSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    heart: HeartSettings = Field(default_factory=HeartSettings)
    brain: BrainSettings = Field(default_factory=BrainSettings)

    @model_validator(mode="after")
    def _refuse_unsafe_bind(self) -> Settings:
        """Hard refusal (issue #5, updated by #24): a non-loopback bind with auth
        disabled must never boot. Raised as `ConfigError` directly rather than
        `ValueError` — this is a model-level check (`loc = ()`), which
        `_format_validation_error`'s `_toml_location` call can't render (it assumes a
        field path). Same pattern `_AnsinaTomlSource` already uses for the
        secret-in-TOML refusal below.

        Keyed on `security.enabled`, not on whether `api_token` happens to be set:
        issue #24 makes an explicit config token optional even when auth *is*
        enabled (Ansina generates its own bootstrap token instead) — `api_token is
        None` is no longer synonymous with "no authentication."

        Runs on every `Settings` construction path (`load_settings()`, env vars, direct
        kwargs), so this can't be bypassed by skipping `load_settings()`.
        """
        if not self.security.enabled and not _is_loopback(self.server.host):
            raise ConfigError(
                _render_report(
                    [
                        f"server.host: refusing to start — bound to "
                        f"{self.server.host!r} (not loopback) with "
                        "security.enabled = false; set ANSINA_SECURITY__ENABLED=true "
                        "or bind [server] host back to 127.0.0.1"
                    ]
                )
            )
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Highest-priority first: init kwargs, then ANSINA_* env vars, then the TOML
        # file — i.e. defaults < file < env, exactly the order issue #2 specifies.
        # `.env` files and Docker-style secret files aren't part of that story, so
        # `dotenv_settings`/`file_secret_settings` are deliberately dropped.
        return (
            init_settings,
            env_settings,
            _AnsinaTomlSource(settings_cls, toml_file=_resolve_config_file()),
        )


def _format_validation_error(
    exc: ValidationError, secret_paths: set[tuple[str, ...]]
) -> str:
    problems = []
    for error in exc.errors():
        loc = tuple(str(part) for part in error["loc"])
        value = "***" if loc in secret_paths else error.get("input")
        problems.append(
            f"{_format_loc(loc)}: {error['msg']} "
            f"(set via {_env_var_name(loc)} or {_toml_location(loc)} in "
            f"{_resolve_config_file()}; got {value!r})"
        )
    return _render_report(problems)


def load_settings(config_file: str | Path | None = None) -> Settings:
    """Load `Settings` from defaults, `ansina.toml` (or `config_file`), and env vars.

    Raises `ConfigError` with one aggregated, readable report if anything is invalid —
    never a bare `pydantic.ValidationError` or `tomllib.TOMLDecodeError`.
    """
    override = Path(config_file) if config_file is not None else None
    token = _config_file_override.set(override)
    try:
        return Settings()
    except ValidationError as exc:
        secret_paths = _walk_secret_paths(Settings)
        raise ConfigError(_format_validation_error(exc, secret_paths)) from exc
    except tomllib.TOMLDecodeError as exc:
        config_file_path = _resolve_config_file()
        raise ConfigError(
            _render_report([f"{config_file_path}: invalid TOML — {exc}"])
        ) from exc
    except SettingsError as exc:
        raise ConfigError(_render_report([str(exc)])) from exc
    finally:
        _config_file_override.reset(token)
