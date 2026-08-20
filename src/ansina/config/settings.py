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
import os
import tomllib
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

    model_config = _MODEL_CONFIG

    path: Path = Path("ansina.db")


class SecuritySettings(BaseModel):
    """Auth material for issue #5. No literal default — ever."""

    model_config = _MODEL_CONFIG

    # `min_length=16` keeps a configured token comfortably above
    # `logging.redaction._MIN_SECRET_LENGTH` (8) — a token too short to clear that floor
    # would be unredactable, i.e. would leak verbatim into any log line that echoes it.
    api_token: SecretStr | None = Field(default=None, min_length=16)


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

    @model_validator(mode="after")
    def _refuse_unsafe_bind(self) -> Settings:
        """Hard refusal (issue #5): a non-loopback bind with auth disabled must never
        boot. Raised as `ConfigError` directly rather than `ValueError` — this is a
        model-level check (`loc = ()`), which `_format_validation_error`'s
        `_toml_location` call can't render (it assumes a field path). Same pattern
        `_AnsinaTomlSource` already uses for the secret-in-TOML refusal below.

        Runs on every `Settings` construction path (`load_settings()`, env vars, direct
        kwargs), so this can't be bypassed by skipping `load_settings()`.
        """
        if self.security.api_token is None and not _is_loopback(self.server.host):
            raise ConfigError(
                _render_report(
                    [
                        f"server.host: refusing to start — bound to "
                        f"{self.server.host!r} (not loopback) with no api_token "
                        "configured; set ANSINA_SECURITY__API_TOKEN or bind "
                        "[server] host back to 127.0.0.1"
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
