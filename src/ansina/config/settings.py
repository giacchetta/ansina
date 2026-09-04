"""Layered, fail-fast configuration for Ansina.

Precedence, lowest to highest: built-in defaults -> ``ansina.toml`` -> ``ANSINA_*``
environment variables. Every subsystem gets its config from a loaded :class:`Settings`
instance — nothing in this codebase calls ``os.getenv`` directly.

Secrets (tokens, API keys) are read from environment variables only. A secret-typed
field (``SecretStr``) set in ``ansina.toml`` is a hard configuration error, not a
silently accepted value — see ``.agents/guardrails/secret-prevention.md``.
"""

from __future__ import annotations

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
    """Tunable argon2id work factors for `ansina.auth.hashing`, consumed by issue #24.

    Defaults follow the OWASP-recommended argon2id baseline (m=64 MiB, t=3, p=4) — high
    enough to make an offline brute-force of a stolen hash expensive, low enough not to
    dominate a login request. The unit suite overrides these with minimal values (see
    `tests/unit/auth/conftest.py`) so hashing doesn't dominate test runtime.
    """

    model_config = _MODEL_CONFIG

    time_cost: int = Field(default=3, ge=1)
    memory_cost_kib: int = Field(default=65536, ge=1)
    parallelism: int = Field(default=4, ge=1)


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


class SecuritySettings(BaseModel):
    """Auth material for issue #5 and #24.

    `enabled=False` is the only way to run with no authentication at all (loopback-only
    — see `Settings._refuse_unsafe_bind`) — a deliberate, explicit opt-out rather than
    an incidental side effect of leaving `api_token` unset. When `enabled=True` (the
    default) and no `api_token` override is configured, Ansina generates its own
    high-entropy bootstrap token on first boot and prints it once
    (`ansina.auth.bootstrap`) — nobody, including Ansina's own logs, ever sees it again.
    """

    model_config = _MODEL_CONFIG

    enabled: bool = True

    # Optional operator override of the auto-generated bootstrap token — e.g. for a
    # scripted/orchestrated install, or CI, that needs a token known ahead of time.
    # No literal default — ever. Validated to look like a securely generated value
    # (length, charset, entropy) rather than a human-chosen phrase; see
    # `_validate_token_strength` below.
    api_token: SecretStr | None = Field(default=None, min_length=_TOKEN_MIN_LENGTH)

    # Issue #24: on first boot with no users, this resolves to a single synthetic Admin
    # identity (`ansina.auth.bootstrap`) so the service stays reachable. Setting this
    # `False` revokes that identity's credential (but keeps the user and its
    # `external_identities` row, for audit-log attribution) once a real Admin account
    # exists — it does not prevent the bootstrap identity from ever being created.
    bootstrap_admin_enabled: bool = True
    password: PasswordHashSettings = Field(default_factory=PasswordHashSettings)
    sudo: SudoSettings = Field(default_factory=SudoSettings)

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
