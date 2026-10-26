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
from collections.abc import Mapping, Sequence
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


class FaultInjectionSettings(BaseModel):
    """A soak/dev-only mode that forces real, scheduled tick faults instead of
    fabricating snapshot text — consumed by issue #72's
    `ansina.heart.tick.fault_injection`. See that module's own docstring for how each
    fault kind is forced for real, and why: every soak run and every day of live
    telemetry published to the bucket before this issue was 100% `idle` — there is no
    non-idle ground truth anywhere in the live corpus to score a tick-decision policy
    against.

    `enabled=False` (the default) is a byte-for-byte no-op — `build_tick_loop` never
    constructs a `FaultInjector`, the exact object graph every pre-#72 caller already
    builds, the same "verified no-op" shape `TickSettings.escalate_to_brain` already
    uses. This is a dev/soak tool, never meant for a production deployment, so
    `enabled=True` with no `schedule_path` fails loudly at boot instead of degrading —
    the same `OidcSettings._validate_enabled_requires_credentials` reasoning, not the
    `S3Settings`/`DevSettings` "misconfigured means a logged skip" shape.
    """

    model_config = _MODEL_CONFIG

    enabled: bool = False
    schedule_path: Path | None = None

    @field_validator("schedule_path")
    @classmethod
    def _resolve_schedule_path(cls, value: Path | None) -> Path | None:
        """Same `~`-expand-and-anchor-to-CWD treatment as
        `HeartSettings._resolve_paths` — a relative `schedule_path` must not silently
        track wherever the process happens to be launched from.
        """
        if value is None:
            return None
        return value.expanduser().resolve()

    @model_validator(mode="after")
    def _validate_enabled_requires_schedule(self) -> FaultInjectionSettings:
        if self.enabled and self.schedule_path is None:
            raise ConfigError(
                _render_report(
                    [
                        "heart.tick.fault_injection.schedule_path: required when "
                        "heart.tick.fault_injection.enabled = true (set "
                        "ANSINA_HEART__TICK__FAULT_INJECTION__SCHEDULE_PATH, or leave "
                        "heart.tick.fault_injection.enabled = false)"
                    ]
                )
            )
        return self


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

    # Issue #54's circuit breaker: the loop auto-pauses once `consecutive_failures` OR
    # `consecutive_overruns` reaches this value — one threshold governs both gauges
    # deliberately (the issue names exactly these three new keys, no fourth).
    max_consecutive_failures: int = Field(default=5, ge=1)
    # A tick counts as an "overrun" once its duration reaches this fraction of
    # `interval_seconds` — tracked independently of failures (a slow-but-succeeding
    # tick never touches `consecutive_failures`).
    overrun_ratio: float = Field(default=0.8, gt=0)
    # An escape hatch to disable the breaker's *automatic* `pause()` call without
    # touching either threshold above — the failure/overrun counters still accrue and
    # stay visible (in the daemon-state snapshot and in logs) either way.
    auto_pause_enabled: bool = True

    # Issue #64: wires an `escalate` decision to `BrainProvider.stream()` (independent
    # of `[brain] enabled` — a `True` flag with the Brain itself off/misconfigured is
    # a "declined" journal outcome, not an error). Defaults `False` — "logged/
    # journal-only" is the byte-for-byte behavior until an operator opts in, mirroring
    # `[brain] enabled`'s own off-by-default shape. Issue #60's own 8-hour soak (zero
    # false `escalate`s, a hard call-volume ceiling already enforced by the tick
    # cadence + the circuit breaker above) is the evidence that no dedup/cooldown rung
    # is needed before turning this on — see `AGENTS.md`'s M7 #64 entry.
    escalate_to_brain: bool = False

    # Issue #72: forces real, scheduled faults during a soak run so the resulting
    # act/escalate decisions have ground truth to score — see `FaultInjectionSettings`.
    fault_injection: FaultInjectionSettings = Field(
        default_factory=FaultInjectionSettings
    )


class JournalSettings(BaseModel):
    """The `heart_journal` table's bounded retention and replay size, consumed by
    issue #55's `ansina.heart.journal`/`ansina.heart.tick.journal_handler`/
    `ansina.heart.tick.sources.recent_journal`.

    Nested under `[heart]`, mirroring `TickSettings` — the journal only ever exists
    alongside a loaded Heart, same reasoning.
    """

    model_config = _MODEL_CONFIG

    # `HeartJournalRepository.append()`'s own sweep keeps the table to at most this
    # many rows — the newest `max_entries` survive, everything else is deleted.
    max_entries: int = Field(default=2000, ge=1)
    # The second, independent sweep bound: a row older than this many days is deleted
    # regardless of `max_entries` — mirrors `LoginAttemptRepository.delete_expired`'s
    # own two-cutoffs reasoning (nothing guarantees one bound is tighter than the
    # other).
    retention_days: int = Field(default=14, ge=1)
    # How many of the most recent rows `RecentJournalSource` replays back into the
    # next tick's snapshot. `0` disables the source's output entirely without
    # unregistering it.
    recent_entries: int = Field(default=5, ge=0)


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
    # Issue #53: the smallest model on the bench ladder that clears the gate
    # (3-way accuracy >= 0.90, 0 false act/escalate on the obviously-idle subset,
    # 0 parse-fallback rate, p95 latency <= 20% of interval_seconds) — measured
    # against `heart.tick.prompts.DEFAULT_PROMPT_VARIANT` ("strict"), 95.8%
    # accuracy, see `docs/heart/bench/` (published to the report bucket once
    # `make heart-bench-publish` has run, issue #59). Superseded the M1-era
    # placeholder default.
    model_repo: str = "mlx-community/gemma-4-e2b-it-4bit"
    cache_dir: Path = Path("~/.cache/ansina/models")
    # The blueprint's 8k context budget is a hard ceiling, not a target (issue #10) —
    # enforced here so it can never be configured past what the Heart's prompts are
    # allowed to assume.
    context_tokens: int = Field(default=8192, ge=256, le=8192)
    max_output_tokens: int = Field(default=512, ge=1)
    # Default `True` (issue #53): every model on the bench ladder is chat/instruct-
    # tuned, and a raw (untemplated) prompt measurably makes one continue the prompt
    # as free text instead of answering it — see the first (no-template) report,
    # `2026-09-29-Qwen3.5-2B-MLX-4bit-baseline-notemplate.{md,json}` (bucket key
    # `kind=bench/dt=2026-09-29/...`, issue #59), 100% parse-fallback.
    # `MlxHeartRuntime` applies the loaded tokenizer's own chat template when this
    # is `True` and the tokenizer exposes one; `False` is the pre-#53 raw-prompt
    # behavior, kept as an escape hatch for a future base (non-instruct) model.
    apply_chat_template: bool = True
    tick: TickSettings = Field(default_factory=TickSettings)
    journal: JournalSettings = Field(default_factory=JournalSettings)

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


class LoginSettings(BaseModel):
    """Login-throttle tuning for `ansina.auth.login_throttle.LoginThrottle`, consumed
    by issue #49 ahead of `POST /auth/login` (#50). Deliberately separate from
    `SudoSettings` above — different caller (an anonymous login attempt, not an
    already-authenticated step-up), different threat model, independently tunable.

    Two independent thresholds, not one: `max_failed_attempts_per_username` catches
    repeated guesses against one account; `max_failed_attempts_per_ip` (a higher
    ceiling, since one IP legitimately serves many users) catches a spray across many
    distinct usernames that never trips any single username bucket.
    """

    model_config = _MODEL_CONFIG

    max_failed_attempts_per_username: int = Field(default=5, ge=1)
    max_failed_attempts_per_ip: int = Field(default=20, ge=1)
    attempt_window_seconds: float = Field(default=900.0, gt=0)
    lockout_seconds: float = Field(default=900.0, gt=0)

    # How long the `api_token` minted at the end of a successful `POST /auth/login`
    # (issue #50) stays valid — mirrors `OidcSettings.token_ttl_seconds`'s own
    # per-feature TTL knob rather than reusing an unrelated one.
    token_ttl_seconds: float = Field(default=3600.0, gt=0)


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


class CorsSettings(BaseModel):
    """Cross-Origin Resource Sharing for browser-hosted third-party clients, consumed
    by issue #51's `ansina.api.cors`.

    `enabled=False` (the default) means `add_cors_middleware` adds nothing at all — the
    same "`None`/no-op when off" shape `[heart]`/`[brain]`/`[security.oidc]` already
    use — so the daemon + `ansina-tui` deployment (which is not a browser and never
    sends an `Origin` header) is byte-for-byte unchanged. `ansina-tui`'s own `ApiClient`
    is unaffected either way: a same-origin, non-browser HTTP client never triggers CORS
    enforcement, which is a browser-side mechanism, not a server-side authorization
    control — `require(resource)` still gates every route exactly as before.

    No `allow_credentials` field: Ansina authenticates with an `Authorization` header,
    never a cookie, so there is nothing for a credentialed CORS mode to protect and it
    would only add CSRF surface for no benefit (`api.cors.add_cors_middleware` always
    passes `allow_credentials=False`).
    """

    model_config = _MODEL_CONFIG

    enabled: bool = False

    # Every entry must be an exact `scheme://host[:port]` origin, the same string a
    # browser sends in its `Origin` header — Starlette compares it exactly. "*" is
    # refused outright (see `_validate_origins` below): a wildcard origin on a
    # credential-bearing API is never the right answer. A trailing slash is stripped
    # (not refused) since it's the same copy-paste footgun
    # `OidcSettings._strip_trailing_slash` already normalizes for `issuer` — a browser's
    # own `Origin` header never carries one, so a configured value that still had one
    # would otherwise silently match nothing, an opaque browser-side failure invisible
    # to Ansina's own logs.
    allowed_origins: tuple[str, ...] = ()

    # `Access-Control-Max-Age` — how long a browser may cache a preflight's answer
    # before repeating it. Starlette's own default (600s) is echoed here explicitly so
    # it's a documented, tunable Ansina setting rather than an implicit library default.
    max_age_seconds: int = Field(default=600, ge=0)

    @field_validator("allowed_origins")
    @classmethod
    def _validate_origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if "*" in value:
            raise ValueError(
                'security.cors.allowed_origins must not contain "*" — every Ansina '
                "route but the health probes and the login routes is authenticated, "
                "and a wildcard origin on a credential-bearing API is never correct; "
                "list each allowed origin explicitly"
            )
        return tuple(origin.rstrip("/") for origin in value)

    @model_validator(mode="after")
    def _validate_enabled_requires_origins(self) -> CorsSettings:
        """`enabled=True` with no `allowed_origins` would add real middleware that
        allows nothing — a silent no-op indistinguishable from a misconfiguration, the
        same "fail loudly, don't discover this on the first browser request" reasoning
        `OidcSettings._validate_enabled_requires_credentials` already applies. Raised
        directly (not `ValueError`) since this is a model-level check with no single
        field `loc` — the same pattern that check and `Settings._refuse_unsafe_bind`
        both use.
        """
        if self.enabled and not self.allowed_origins:
            raise ConfigError(
                _render_report(
                    [
                        "security.cors.allowed_origins: required (non-empty) when "
                        "security.cors.enabled = true — set "
                        "ANSINA_SECURITY__CORS__ALLOWED_ORIGINS to a JSON array of "
                        "allowed origins, or leave security.cors.enabled = false"
                    ]
                )
            )
        return self


class S3Settings(BaseModel):
    """S3-compatible object store for the Heart bench/soak report corpus, consumed by
    issue #59's `ansina.heart.eval.storage`. Also the one config surface issue #61's
    telemetry producer and issue #62's Vector-sidecar supervision are expected to
    read from, so it lives at `[telemetry.s3]`, not nested under `[heart]`.

    `enabled=False` (the default) means `build_report_storage` returns `None` and
    nothing in `heart/eval/__main__.py`/`heart/eval/publish.py` attempts a network
    call — same shape as `[heart]`/`[brain]`/`[security.oidc]`/`[security.cors]`.

    Deliberately **no** `model_validator` refusing `enabled=True` with an incomplete
    config, unlike `OidcSettings`/`CorsSettings` above: issue #59's own AC requires a
    missing bucket or bad credentials to degrade to a *logged* failure at upload time,
    never a boot-time `ConfigError` that would make `make heart-bench`'s own gate
    verdict unreachable. `build_report_storage` is what validates coherence instead.

    "S3-compatible," not "AWS" — `endpoint_url` lets Cloudflare R2 (the M7 lab/
    acceptance target), MinIO, Backblaze B2, Wasabi, or GCP Cloud Storage's S3/XML
    interop stand in for AWS S3 itself, the same way `[brain] base_url` already lets
    any OpenAI-compatible endpoint stand in for OpenAI. Empty string means "use
    boto3's own default AWS endpoint resolution."

    `access_key_id`/`secret_access_key` are env-only `SecretStr`s, the same hard
    "secret in `ansina.toml` is a startup error" rule every other Ansina secret
    follows (enforced generically by `_AnsinaTomlSource`/`_walk_secret_paths`, no new
    code needed here). Ansina does not fall back to boto3's ambient credential chain
    (environment/shared-config/instance-profile) — both fields are required together
    once `enabled=True`, the same "nothing calls `os.getenv` directly" discipline
    every other subsystem in this codebase follows.
    """

    model_config = _MODEL_CONFIG

    enabled: bool = False
    bucket: str = ""
    endpoint_url: str = ""
    # Cloudflare R2 requires exactly this value; a real AWS deployment overrides it
    # with a genuine region name.
    region: str = "auto"
    key_prefix: str = ""
    access_key_id: SecretStr | None = None
    secret_access_key: SecretStr | None = None


class TelemetrySettings(BaseModel):
    """Umbrella for `[telemetry.*]`: the object store (`s3`, issue #59) plus the
    flag-gated local producer (issue #61, `ansina.telemetry`) — a continuous,
    file-only RSS/tick/decision sample stream plus a redacted log mirror, both
    bounded by the same three-way budget below. `enabled = false` (the default) is
    a byte-for-byte no-op: no spool directory is created, no log-mirror handler is
    attached (`ansina.logging.setup.configure_logging`), and no sampling loop starts
    (`ansina.api.app.create_app`) — same "off means nothing happens" shape
    `[heart]`/`[security.oidc]`/`[security.cors]` already use. Issue #62's Vector
    sidecar config will land as a further sibling under this same table, not under
    `[security]`/`[heart]`.

    Telemetry never writes to SQLite and never uploads anything itself (that's
    issue #62's sidecar and `s3`'s own `heart.eval.storage`/`heart.eval.publish`) —
    files only, and strictly local.
    """

    # `validate_default=True` (unlike the shared `_MODEL_CONFIG`) so the *default*
    # `spool_dir` goes through `_resolve_spool_dir` too — same reasoning
    # `DatabaseSettings`/`HeartSettings` already document for their own paths.
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    enabled: bool = False

    # Issue #67: the deployment identifier `ansina.dev.vector.resolve_host_id`
    # injects into the `host=<id>/` key-prefix segment for both Vector sinks
    # (`kind=telemetry/`, `kind=runlog/`) — without it, two deployments shipping
    # to the same `[telemetry.s3]` bucket are indistinguishable. Left raw here
    # (no normalization at this layer, same as `S3Settings.key_prefix`); `""` (the
    # default) means "resolve this host's own `socket.gethostname()` instead,"
    # resolved lazily at env-build time, not baked into this default — the same
    # "resolved by the module that actually needs it, not at settings-load time"
    # reasoning `DevSettings.vector_binary`'s own docstring already documents for
    # a bare-name default.
    host_id: str = ""

    # Where rotated sample/log files are written. Never created by this settings
    # model itself (only resolved to an absolute path) — `enabled = false` must
    # create nothing, so the directory is only ever made lazily, by
    # `ansina.telemetry.rotation.SpooledRotatingWriter`'s own first write, and only
    # when something is actually enabled to write.
    spool_dir: Path = Path("~/.local/state/ansina/telemetry")

    # Matches `scripts/heart-soak-run.sh`'s own sampling cadence (issue #56).
    sample_interval_seconds: float = Field(default=60.0, gt=0)

    # Per rotated-file-family budget (one family for samples, one for the log
    # mirror — see `ansina.telemetry.rotation`'s own module docstring for why each
    # family is bounded independently rather than sharing one combined pool).
    # 1 MiB/file, 100 MiB/family total, 7 days — generous headroom against the
    # issue's own measured ~15 MB/week combined real rate, against a 10 GB/week
    # target ceiling.
    max_file_bytes: int = Field(default=1_048_576, gt=0)
    max_spool_bytes: int = Field(default=104_857_600, gt=0)
    # Independent of `max_spool_bytes`, mirroring `LoginAttemptRepository.
    # delete_expired`'s own two-cutoffs reasoning: nothing guarantees one bound is
    # tighter than the other, so a file failing either test is deleted.
    retention_hours: int = Field(default=168, ge=1)

    s3: S3Settings = Field(default_factory=S3Settings)

    @field_validator("spool_dir")
    @classmethod
    def _resolve_spool_dir(cls, value: Path) -> Path:
        """Same `~`-expand-and-anchor-to-CWD treatment as
        `DatabaseSettings._resolve_path`/`HeartSettings._resolve_paths`.
        """
        return value.expanduser().resolve()

    @model_validator(mode="after")
    def _validate_file_fits_in_spool(self) -> TelemetrySettings:
        """A rotated file that could never fit under its own family's total cap is
        a configuration error, not something to discover later at rotation time —
        the same "fail loudly, don't discover this on the first real request"
        reasoning `CorsSettings`/`OidcSettings` already apply to their own
        enabled-requires-coherent-config checks. Raised directly (not `ValueError`):
        a model-level check with no single field `loc`, same pattern those two use.
        """
        if self.max_file_bytes > self.max_spool_bytes:
            raise ConfigError(
                _render_report(
                    [
                        "telemetry.max_file_bytes: must be <= "
                        "telemetry.max_spool_bytes (got "
                        f"max_file_bytes={self.max_file_bytes}, "
                        f"max_spool_bytes={self.max_spool_bytes})"
                    ]
                )
            )
        return self


class DevSettings(BaseModel):
    """`[dev]` — the lab/pre-customer posture (issue #62), never a default.

    `enabled = false` (the default) is a byte-for-byte no-op: `ansina.dev.dev_mode`
    yields immediately with nothing logged and nothing spawned. `enabled = true` is
    set only by `python -m ansina --dev` (`ANSINA_DEV__ENABLED=true` works too, since
    every setting is env-reachable) — established in `__main__.py` before uvicorn
    binds a port, never inside `create_app()`/its lifespan, so the app factory and
    every subprocess-launched test (`tests/e2e`, which never pass `--dev`) see zero
    change.

    Deliberately **no** `model_validator` refusing `enabled=True` with Vector missing
    or `[telemetry]`/`[telemetry.s3]` incoherent, the same reasoning `S3Settings`
    documents for its own `enabled=True`: every one of `ansina.dev.vector.preflight`'s
    checks is a *logged skip*, never a boot-time `ConfigError` — a lab posture must
    still boot the daemon normally when its sidecar can't come up.

    `vector_binary` must resolve to a **0.45.x** build — see `ansina.dev.vector`'s
    own module-level comment for why every later release is incompatible with
    Cloudflare R2 (an empirical finding, not a guess: reproduced against this
    project's own bucket, tracked upstream at vectordotdev/vector#23029).
    """

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    enabled: bool = False

    # Resolved via `shutil.which` at preflight time, not here — a name (the common
    # case) must stay a `PATH` lookup, not get silently turned into `None`/an error
    # by this model just because it isn't yet an absolute path on this machine.
    vector_binary: str = "vector"

    # The committed, hand-editable sidecar config (issue #62's "Vector config
    # ownership" — a static file, never daemon-generated, so there is no second copy
    # to drift). Same `~`-expand-and-anchor-to-CWD treatment as `DatabaseSettings.path`.
    vector_config: Path = Path("deploy/vector.toml")

    # One post-spawn liveness check, no continuous supervision loop (per the issue's
    # own "Dev Mode" scope) — this is how long after spawn that one check waits.
    liveness_delay_seconds: float = Field(default=1.0, gt=0)

    # `terminate()` -> wait this long -> `kill()`, guaranteeing no orphan survives a
    # deliberate `dev_mode()` exit (SIGKILL to the daemon itself is a separate,
    # documented limitation — see `ansina.dev.posture`).
    shutdown_timeout_seconds: float = Field(default=5.0, gt=0)

    # Bounds the one-time `vector validate --no-environment` preflight call — kept
    # short since it never touches the network (`--no-environment` skips the sinks'
    # own connectivity check), just config parsing.
    validate_timeout_seconds: float = Field(default=30.0, gt=0)

    @field_validator("vector_config")
    @classmethod
    def _resolve_vector_config(cls, value: Path) -> Path:
        return value.expanduser().resolve()


_ANSINA_PROJECT_NAME = "ansina"


def _project_name_at(pyproject_path: Path) -> str | None:
    """`[project] name` from a `pyproject.toml`, or `None` on anything that
    doesn't parse as one — a malformed/foreign TOML file at that path must never
    raise out of a settings validator, only fail to match.
    """
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, tomllib.TOMLDecodeError:
        return None
    project = data.get("project")
    if isinstance(project, dict):
        name = project.get("name")
        if isinstance(name, str):
            return name
    return None


def _repo_root_named(start: Path, name: str) -> Path | None:
    """Walks `start` and every parent looking for a `pyproject.toml` whose
    `[project] name` is exactly `name`, returning the directory that holds it —
    or `None` once the walk reaches the filesystem root with no match. Bounded
    (one `pyproject.toml` read per directory on the path to `/`), never an
    unbounded subtree scan.
    """
    for candidate in (start, *start.parents):
        pyproject = candidate / "pyproject.toml"
        if pyproject.is_file() and _project_name_at(pyproject) == name:
            return candidate
    return None


class OpencodePermissionSettings(BaseModel):
    """`[opencode.permission]` — the deny-by-default permission block
    `ansina.opencode.config` renders into OpencodeConfig's own `opencode.json`,
    consumed by issue #74's `ansina.opencode.config`.

    Ansina's own typed vocabulary, not OpenCode's: `edit`/`bash` are
    `"deny"`/`"allow"`/one Ansina-defined confinement mode (`"workspace"`,
    `"allowlist"`) rather than OpenCode's raw glob-map shape — `opencode.config`
    is what translates this into OpenCode's own `{"*": ..., "<glob>": ...}`
    object. Every other key is plain `"allow"`/`"deny"`. `"ask"` is
    unrepresentable by construction anywhere in this model — a headless
    `--auto` run has nobody to answer a prompt, so an `"ask"` value would hang
    until the timeout rather than fail fast; `additional` (below) is typed the
    same restrictive way for the identical reason.

    `additional` reaches any of OpenCode's other documented permission keys
    (`glob`, `grep`, `task`, `todowrite`, `skill`, `lsp`, `question`,
    `doom_loop`, ...) this model doesn't name explicitly, without a code change
    — forward-compatible with a new OpenCode permission kind the same way
    `heart.triage`'s `other` event-type fallback is forward-compatible with a
    new OpenCode *event* kind.
    """

    model_config = _MODEL_CONFIG

    default: Literal["allow", "deny"] = "deny"
    read: Literal["allow", "deny"] = "allow"
    edit: Literal["deny", "allow", "workspace"] = "workspace"
    bash: Literal["deny", "allow", "allowlist"] = "allowlist"
    bash_allowlist: tuple[str, ...] = (
        "git status*",
        "git diff*",
        "git log*",
        "ls*",
        "cat*",
        "rg*",
    )
    bash_denylist: tuple[str, ...] = (
        "rm *",
        "sudo *",
        "curl *",
        "wget *",
        "ssh *",
        "git push*",
    )
    webfetch: Literal["allow", "deny"] = "deny"
    websearch: Literal["allow", "deny"] = "deny"
    external_directory: Literal["allow", "deny"] = "deny"
    additional: Mapping[str, Literal["allow", "deny"]] = Field(default_factory=dict)


# Refused in `inherit_env_names` below — Ansina's own settings-env namespace,
# never meant to reach a child process under cover of an operator-named
# allow-list entry.
_INHERIT_ENV_FORBIDDEN_PREFIX = "ANSINA_"
# Secret-shaped, regardless of namespace — the same credential-smell patterns
# `ansina.logging.redaction` (naming, not pattern-matching) and
# `ansina.dev.vector.build_vector_env`'s own hand-picked allow-list already treat
# as sensitive. Matched case-insensitively against the *whole* name, not just a
# prefix/suffix — `secrets.token_urlsafe` isn't the only way a name can carry a
# credential.
_INHERIT_ENV_SECRET_SHAPED_PATTERNS = (
    re.compile(r"^AWS_", re.IGNORECASE),
    re.compile(r"_API_KEY$", re.IGNORECASE),
    re.compile(r"SECRET", re.IGNORECASE),
    re.compile(r"TOKEN", re.IGNORECASE),
)


def _looks_unsafe_to_inherit(name: str) -> bool:
    if name.upper().startswith(_INHERIT_ENV_FORBIDDEN_PREFIX):
        return True
    return any(pattern.search(name) for pattern in _INHERIT_ENV_SECRET_SHAPED_PATTERNS)


class OpencodeSettings(BaseModel):
    """`[opencode]` — the confined OpenCode workspace/config/env foundation for
    M9's "Hands" executor, consumed by issue #74's `ansina.opencode`.

    `enabled=False` (the default) is a byte-for-byte no-op: `ansina.opencode`'s
    own `preflight`/`ensure_workspace`/`build_opencode_service` create no
    directory, write no file, and log nothing — the same "`None`/no-op when
    off" shape `[heart]`/`[brain]`/`[security.oidc]` already use. Issue #74
    shipped this confinement foundation with no executor at all; issue #75's
    `ansina.opencode.runner.OpencodeCliRunner` is the first thing that actually
    spawns a real `opencode` child to do work, consuming `run_timeout_seconds`/
    `shutdown_timeout_seconds`/`max_concurrent_runs`/`queue_max_depth`/
    `max_prompt_chars`/`max_event_bytes`/`max_run_output_bytes`/`model`/`agent`/
    `auto_approve` below — still wired into no route or tick-loop caller as of
    that issue (see `ansina/opencode/`'s own module docstrings).

    `workspace_dir` goes through the same `~`-expand-and-`.resolve()`
    `field_validator` as `DatabaseSettings.path`/`TelemetrySettings.spool_dir`,
    `validate_default=True` for the same reason those two document (the
    *default* path must resolve too, not just an explicit one). A further
    `model_validator` (`_validate_workspace_outside_ansina_checkout` below)
    refuses a `workspace_dir` that is, is inside, or contains an ansina
    checkout — OpenCode's own `bash`/`edit` reach inside this directory is
    broad even under `[opencode.permission]`'s own confinement, so pointing it
    at Ansina's own source would defeat every other control in this file.

    `provider_api_key` is env-only, the same "`SecretStr` in TOML is a startup
    error" rule every other Ansina secret follows (`_AnsinaTomlSource`/
    `_walk_secret_paths` already enforce this generically — no new code is
    needed for it here). `inherit_env_names` is validated the opposite
    direction: it refuses an entry that could smuggle a secret into the
    child's environment under cover of this allow-list (see
    `_looks_unsafe_to_inherit` above).

    See `Settings._refuse_unobserved_executor` for the cross-section rule this
    model's own `enabled` flag participates in — `[opencode] enabled = true`
    with `[telemetry] enabled = false` is refused at the `Settings` level, not
    here, since this model alone can't see `[telemetry]`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    enabled: bool = False
    binary: str = "opencode"
    # Empty means "accept whatever `opencode --version` reports" — set this to
    # pin a known-good release the way `[dev].vector_binary`'s own docstring
    # pins Vector to 0.45.x after finding a real incompatibility the hard way.
    # Ansina targets OpenCode v2, not v1 (see `make opencode-install`/`docs/
    # opencode.md`) — a raw-prefix match against the trimmed `--version`
    # output, not a semver comparison, since the output's own shape is not
    # stable across OpenCode major versions (v1 prints a bare `"1.19.3"`; v2
    # prints `"opencode v2.1.1"`, with the binary name and a leading `v`).
    # `"opencode v2."` is the expected shape once a v2 release is actually
    # validated against Ansina; `ansina.opencode.preflight`'s own module
    # docstring states the general rule plainly rather than assuming one
    # shape, since a bare numeric prefix (e.g. `"2."`) would never match v2's
    # own output.
    required_version_prefix: str = ""
    workspace_dir: Path = Path("~/.local/state/ansina/opencode")
    # `True` redirects the child's own `HOME` into an isolated directory inside
    # `workspace_dir` — see `ansina.opencode.env`'s own module docstring for why
    # this matters beyond tidiness: OpenCode's global
    # `~/.config/opencode/opencode.json` merges *under* project config, so an
    # ambient operator-level config on the host could otherwise loosen this
    # deny-by-default block for every key our own generated config doesn't
    # explicitly name.
    isolate_home: bool = True
    # The env var name (inside the *child's* environment) that names the
    # provider API key — e.g. "ANTHROPIC_API_KEY". Empty means "no key passed."
    provider_api_key_env: str = ""
    provider_api_key: SecretStr | None = Field(default=None)
    # Operator-named extra environment variables to forward verbatim from the
    # daemon's own environment into the child's — empty by default, the same
    # "explicit allow-list, never the daemon's own os.environ wholesale" shape
    # `ansina.dev.vector.build_vector_env`'s `_INHERITED_ENV_NAMES` already
    # uses. See `_looks_unsafe_to_inherit` above for what's refused outright.
    inherit_env_names: tuple[str, ...] = ()
    preflight_timeout_seconds: float = Field(default=30.0, gt=0)
    # Issue #75 (`ansina.opencode.runner`) — a run's own wall-clock cap, and the
    # terminate->kill grace window once it's hit. `shutdown_timeout_seconds`
    # mirrors `[dev] shutdown_timeout_seconds`'s own name/shape (`ansina.dev.
    # sidecar.SupervisedProcess.stop`) — same terminate-then-bounded-wait-then-
    # kill discipline, applied to a per-run child instead of one long-lived
    # sidecar.
    run_timeout_seconds: float = Field(default=900.0, gt=0)
    shutdown_timeout_seconds: float = Field(default=10.0, gt=0)
    # Issue #75 — `asyncio.Semaphore(max_concurrent_runs)` bounds how many
    # `opencode` children may actually be running at once; `queue_max_depth`
    # bounds the total number of `OpencodeCliRunner.run()` calls admitted
    # (waiting on that semaphore, or already running) before a further call is
    # refused outright (`OpencodeBusyError`) rather than queued unboundedly.
    # `queue_max_depth >= max_concurrent_runs` is enforced below — a backlog
    # smaller than the concurrency it's meant to queue behind would make no
    # call ever actually wait, silently turning the queue into a no-op.
    max_concurrent_runs: int = Field(default=1, ge=1)
    queue_max_depth: int = Field(default=10, ge=1)
    # Issue #75 — three independent size caps, each guarding a different
    # resource: `max_prompt_chars` refuses an oversized request before any
    # subprocess exists at all (`OpencodeRunFailedError`); `max_event_bytes`
    # bounds what a single captured JSON line contributes to a `RunEvent`
    # after truncation+redaction (`ansina.opencode.events.parse_run_event`);
    # `max_run_output_bytes` bounds the whole run's own captured output.
    # `max_event_bytes <= max_run_output_bytes` is enforced below — a single
    # line allowed to exceed the whole-run cap would make that cap
    # unenforceable on its very first oversized line.
    max_prompt_chars: int = Field(default=100_000, gt=0)
    max_event_bytes: int = Field(default=65_536, gt=0)
    max_run_output_bytes: int = Field(default=10_485_760, gt=0)
    # Issue #75 — per-run `--model`/`--agent` defaults; `""` omits the flag
    # entirely (the same "empty means unset" shape `provider_api_key_env`
    # above already uses), letting `opencode` fall back to its own configured
    # default. `ansina.opencode.events.RunRequest.model`/`.agent` override
    # these per call without changing this section's own configured default.
    model: str = ""
    agent: str = ""
    # Issue #75 — maps to `opencode run --auto` ("auto-approve permissions
    # that are not explicitly denied"). Defaults to `True`, not `False` like
    # every other `[opencode]`/`[dev]`/`[telemetry]` `enabled` flag in this
    # file: a headless, non-interactive run has nobody to answer a prompt
    # `--auto` would otherwise leave standing — `ansina.opencode.preflight`'s
    # own module docstring already reasons as if `--auto` is always passed
    # ("a headless `--auto` run has nobody to answer a prompt, so even one
    # surviving `'ask'` ... would hang a real run until its own timeout").
    # The deny-by-default permission floor and the effective-permission
    # preflight check are what actually confine the child; `--auto` only
    # changes whether an already-resolved allow/deny needs a human to confirm
    # it, which this deployment shape never has.
    auto_approve: bool = True
    permission: OpencodePermissionSettings = Field(
        default_factory=OpencodePermissionSettings
    )

    @model_validator(mode="after")
    def _validate_run_limits(self) -> OpencodeSettings:
        """Two cross-field invariants, same `TelemetrySettings.max_file_bytes
        <= max_spool_bytes` shape — each pairs a tighter-scoped cap with the
        broader one it must never exceed, or the broader cap could never
        actually bind.
        """
        if self.queue_max_depth < self.max_concurrent_runs:
            raise ConfigError(
                _render_report(
                    [
                        "opencode.queue_max_depth: must be >= "
                        "opencode.max_concurrent_runs (got "
                        f"queue_max_depth={self.queue_max_depth}, "
                        f"max_concurrent_runs={self.max_concurrent_runs})"
                    ]
                )
            )
        if self.max_event_bytes > self.max_run_output_bytes:
            raise ConfigError(
                _render_report(
                    [
                        "opencode.max_event_bytes: must be <= "
                        "opencode.max_run_output_bytes (got "
                        f"max_event_bytes={self.max_event_bytes}, "
                        f"max_run_output_bytes={self.max_run_output_bytes})"
                    ]
                )
            )
        return self

    @field_validator("workspace_dir")
    @classmethod
    def _resolve_workspace_dir(cls, value: Path) -> Path:
        """Same `~`-expand-and-anchor-to-CWD treatment as
        `DatabaseSettings._resolve_path`/`TelemetrySettings._resolve_spool_dir`.
        """
        return value.expanduser().resolve()

    @field_validator("inherit_env_names")
    @classmethod
    def _validate_inherit_env_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        rejected = [name for name in value if _looks_unsafe_to_inherit(name)]
        if rejected:
            raise ValueError(
                "opencode.inherit_env_names must not include an ANSINA_-prefixed "
                "or secret-shaped (AWS_*/*_API_KEY/*SECRET*/*TOKEN*) name — "
                f"rejected: {', '.join(rejected)}"
            )
        return value

    @model_validator(mode="after")
    def _validate_workspace_outside_ansina_checkout(self) -> OpencodeSettings:
        """`workspace_dir` is handed to a repeatedly-spawned external child with
        broad `bash`/`edit` reach inside it — pointing it at (or around) this
        very checkout would let that child read or modify Ansina's own source,
        defeating every other confinement control in this file. Gated on
        `enabled`: an unused default must never fail to load just because it
        happens to sit near an unrelated checkout on this machine.

        Two independent, bounded probes, never an unbounded subtree scan: (1)
        does `workspace_dir` itself sit at or inside a directory that is an
        ansina checkout (walking *up* from it)? (2) does this running code's
        own checkout (walking up from this very file — deliberately not
        `Path.cwd()`, which a test's own `monkeypatch.chdir` would otherwise
        make this check silently miss, and which a real deployment's own
        working directory need not be the checkout at all) sit inside
        `workspace_dir`? Either direction is refused as a `ConfigError`
        (model-level, no single field `loc` — the
        `_validate_enabled_requires_schedule` pattern).
        """
        if not self.enabled:
            return self
        workspace = self.workspace_dir
        if _repo_root_named(workspace, _ANSINA_PROJECT_NAME) is not None:
            raise ConfigError(
                _render_report(
                    [
                        f"opencode.workspace_dir: {workspace} is, or is inside, "
                        "an ansina checkout — point it at a directory with no "
                        "ansina source in it or above it"
                    ]
                )
            )
        this_repo_root = _repo_root_named(
            Path(__file__).resolve(), _ANSINA_PROJECT_NAME
        )
        if this_repo_root is not None and this_repo_root.is_relative_to(workspace):
            raise ConfigError(
                _render_report(
                    [
                        f"opencode.workspace_dir: {workspace} contains this "
                        f"running ansina checkout ({this_repo_root}) — point it "
                        "at a directory with no ansina source in it or above it"
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
    login: LoginSettings = Field(default_factory=LoginSettings)
    encryption: EncryptionSettings = Field(default_factory=EncryptionSettings)
    oidc: OidcSettings = Field(default_factory=OidcSettings)
    cors: CorsSettings = Field(default_factory=CorsSettings)

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
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    dev: DevSettings = Field(default_factory=DevSettings)
    opencode: OpencodeSettings = Field(default_factory=OpencodeSettings)

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

    @model_validator(mode="after")
    def _refuse_unobserved_executor(self) -> Settings:
        """`[opencode] enabled = true` with `[telemetry] enabled = false` must
        never boot — the `OidcSettings`/`CorsSettings` fail-loudly posture,
        deliberately **not** the `S3Settings`/`DevSettings` degrade-gracefully
        one those two models' own docstrings document.

        The difference in posture is deliberate, not an inconsistency: every
        other "degrade gracefully" subsystem in this file (`[telemetry.s3]`,
        `[dev]`) only ever *ships* something that was already captured — a
        missing bucket or a dead sidecar means a bench/soak report or a
        telemetry file sits unshipped, recoverable later (`heart-bench-
        publish`'s whole job). An unobserved OpenCode run is different in kind:
        every tool call, permission denial, and outcome it produces is
        emitted *as telemetry or nothing at all* (issue #76) — there is no
        local file sitting around to re-ship after the fact. A run that
        happens with telemetry off is not merely unshipped, it never existed
        anywhere Ansina can account for it, which is the one thing this
        milestone refuses to allow for an executor with real `bash`/`edit`
        reach. Raised as `ConfigError` directly (model-level, no single field
        `loc`) — the same pattern `_refuse_unsafe_bind` above and
        `OidcSettings`/`CorsSettings`'s own `enabled`-pairing checks use.
        """
        if self.opencode.enabled and not self.telemetry.enabled:
            raise ConfigError(
                _render_report(
                    [
                        "opencode.enabled: refusing to start — "
                        "telemetry.enabled must be true whenever opencode.enabled "
                        "is true (an executor Ansina cannot observe is refused "
                        "outright); set ANSINA_TELEMETRY__ENABLED=true or leave "
                        "opencode.enabled = false"
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


def load_settings(
    config_file: str | Path | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> Settings:
    """Load `Settings` from defaults, `ansina.toml` (or `config_file`), and env vars.

    `overrides` (new, issue #62) is passed as `Settings(**overrides)` — the designed
    highest-priority source `settings_customise_sources` already ranks `init_settings`
    as, not a second, parallel override mechanism. `__main__.py`'s `--dev` flag is the
    one real caller (`overrides={"dev": {"enabled": True}}`): a nested dict merges
    with env/file values for every other field exactly the way two config sources
    already merge with each other, so passing `{"dev": {...}}` can never shadow an
    unrelated table like `[security]`.

    Raises `ConfigError` with one aggregated, readable report if anything is invalid —
    never a bare `pydantic.ValidationError` or `tomllib.TOMLDecodeError`.
    """
    override = Path(config_file) if config_file is not None else None
    token = _config_file_override.set(override)
    try:
        return Settings(**(overrides or {}))
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
