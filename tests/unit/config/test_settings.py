from pathlib import Path

import pytest
from pydantic import ValidationError

from ansina.config import ConfigError, load_settings
from ansina.config.settings import (
    SecuritySettings,
    ServerSettings,
    _token_entropy_bits_per_char,
    _unwrap_model,
)


def test_defaults_only(clean_env: None, tmp_cwd: Path) -> None:
    """No file, no env -> every documented default."""
    settings = load_settings()

    assert settings.server.host == "127.0.0.1"
    assert settings.server.port == 8000
    assert settings.logging.level == "INFO"
    assert settings.database.path == tmp_cwd / "ansina.db"
    assert settings.security.enabled is True
    assert settings.security.api_token is None
    assert settings.security.bootstrap_admin_enabled is True
    assert settings.security.password.time_cost == 3
    assert settings.security.password.memory_cost_kib == 65536
    assert settings.security.password.parallelism == 4
    assert settings.heart.enabled is False
    assert settings.heart.runtime == "auto"
    assert settings.heart.model_path is None
    assert settings.heart.model_repo == "mlx-community/Qwen3-4B-Instruct-2507-4bit"
    assert settings.heart.cache_dir == Path.home() / ".cache" / "ansina" / "models"
    assert settings.heart.context_tokens == 8192
    assert settings.heart.max_output_tokens == 512
    assert settings.heart.tick.enabled is True
    assert settings.heart.tick.interval_seconds == 30.0
    assert settings.heart.tick.jitter_seconds == 3.0
    assert settings.brain.enabled is False
    assert settings.brain.base_url == "https://api.openai.com/v1"
    assert settings.brain.model == "gpt-4o-mini"
    assert settings.brain.api_key is None
    assert settings.brain.timeout_seconds == 60.0
    assert settings.brain.max_output_tokens == 2048
    assert settings.brain.max_retries == 3
    assert settings.brain.retry_initial_backoff_seconds == 1.0
    assert settings.brain.retry_max_backoff_seconds == 30.0
    assert settings.brain.price_per_1m_input_tokens is None
    assert settings.brain.price_per_1m_output_tokens is None


def test_toml_overrides_defaults(clean_env: None, tmp_cwd: Path) -> None:
    (tmp_cwd / "ansina.toml").write_text("[server]\nport = 8100\n", encoding="utf-8")

    settings = load_settings()

    assert settings.server.port == 8100
    assert settings.server.host == "127.0.0.1"  # untouched key keeps its default


def test_env_overrides_toml(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_cwd / "ansina.toml").write_text("[server]\nport = 8100\n", encoding="utf-8")
    monkeypatch.setenv("ANSINA_SERVER__PORT", "9000")

    settings = load_settings()

    assert settings.server.port == 9000


def test_explicit_config_file_beats_env_var_beats_default_path(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_cwd / "ansina.toml").write_text("[server]\nport = 1111\n", encoding="utf-8")

    env_pointed = tmp_cwd / "env-pointed.toml"
    env_pointed.write_text("[server]\nport = 2222\n", encoding="utf-8")
    monkeypatch.setenv("ANSINA_CONFIG_FILE", str(env_pointed))

    # ANSINA_CONFIG_FILE beats the default ./ansina.toml.
    assert load_settings().server.port == 2222

    explicit = tmp_cwd / "explicit.toml"
    explicit.write_text("[server]\nport = 3333\n", encoding="utf-8")

    # An explicit argument beats ANSINA_CONFIG_FILE.
    assert load_settings(config_file=explicit).server.port == 3333


def test_multiple_invalid_fields_report_together(
    clean_env: None, tmp_cwd: Path
) -> None:
    (tmp_cwd / "ansina.toml").write_text(
        '[server]\nport = "eighty"\n\n[logging]\nlevel = "LOUD"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc_info:
        load_settings()

    message = str(exc_info.value)
    assert "2 problems" in message
    assert "server.port" in message
    assert "logging.level" in message


def test_unknown_toml_key_rejected(clean_env: None, tmp_cwd: Path) -> None:
    (tmp_cwd / "ansina.toml").write_text(
        "[server]\nnot_a_real_field = 1\n", encoding="utf-8"
    )

    with pytest.raises(ConfigError):
        load_settings()


def test_malformed_toml_raises_config_error(clean_env: None, tmp_cwd: Path) -> None:
    (tmp_cwd / "ansina.toml").write_text("this is not [valid toml", encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_settings()

    assert "invalid TOML" in str(exc_info.value)


def test_secret_in_toml_file_rejected(clean_env: None, tmp_cwd: Path) -> None:
    (tmp_cwd / "ansina.toml").write_text(
        '[security]\napi_token = "leaked-token"\n', encoding="utf-8"
    )

    with pytest.raises(ConfigError) as exc_info:
        load_settings()

    message = str(exc_info.value)
    assert "ANSINA_SECURITY__API_TOKEN" in message
    assert "leaked-token" not in message


# A value that clears `SecuritySettings`'s strength bar (>=32 chars, base64url
# charset, >=2.5 bits/char of its own character-distribution entropy) — see
# `config/settings.py`'s `_TOKEN_MIN_LENGTH`/`_TOKEN_CHARSET`/
# `_TOKEN_MIN_ENTROPY_BITS_PER_CHAR`.
_STRONG_TOKEN = "s3cr3t-value-0123456789-abcdefgh"


def test_secret_via_env_loads_and_never_appears_in_text(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_SECURITY__API_TOKEN", _STRONG_TOKEN)

    settings = load_settings()

    assert settings.security.api_token is not None
    assert settings.security.api_token.get_secret_value() == _STRONG_TOKEN
    assert _STRONG_TOKEN not in repr(settings)
    assert _STRONG_TOKEN not in str(settings)


def test_secret_not_leaked_in_error_report_for_sibling_failure(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_SECURITY__API_TOKEN", _STRONG_TOKEN)
    toml_path = tmp_cwd / "ansina.toml"
    toml_path.write_text('[server]\nport = "eighty"\n', encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_settings()

    assert _STRONG_TOKEN not in str(exc_info.value)


def test_short_api_token_rejected(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_SECURITY__API_TOKEN", "too-short")

    with pytest.raises(ConfigError) as exc_info:
        load_settings()

    message = str(exc_info.value)
    assert "security.api_token" in message
    assert "too-short" not in message


def test_bad_charset_api_token_rejected(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long, high-entropy value is still rejected if it isn't URL-safe-base64-shaped
    — e.g. a copy-pasted human phrase with spaces/punctuation."""
    monkeypatch.setenv(
        "ANSINA_SECURITY__API_TOKEN", "Hello, World! this has punctuation & spaces 42"
    )

    with pytest.raises(ConfigError) as exc_info:
        load_settings()

    message = str(exc_info.value)
    assert "security.api_token" in message
    assert "letters, digits" in message


def test_low_entropy_api_token_rejected(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Long enough (32 chars) and charset-clean, but a 2-character repeating
    pattern — rejected on entropy, not length or charset."""
    monkeypatch.setenv("ANSINA_SECURITY__API_TOKEN", "ab" * 16)

    with pytest.raises(ConfigError) as exc_info:
        load_settings()

    message = str(exc_info.value)
    assert "security.api_token" in message
    assert "too predictable" in message


def test_token_entropy_of_empty_string_is_zero() -> None:
    """Guards `_validate_token_strength` against a division by zero — unreachable via
    `load_settings()` today (the `min_length` constraint rejects an empty token before
    the entropy check ever runs), but this is a general-purpose helper, not a
    single-call-site inline detail, so it must behave sanely called directly too.
    """
    assert _token_entropy_bits_per_char("") == 0.0


def test_security_settings_accepts_an_explicit_none_token() -> None:
    """`_validate_token_strength`'s `value is None` branch — not reachable via
    `load_settings()` (an *absent* env var means "use the default," which pydantic
    never routes through this validator; only an *explicitly provided* value is), but
    `SecuritySettings` can still be constructed directly with `api_token=None`.
    """
    settings = SecuritySettings(api_token=None)

    assert settings.api_token is None


def test_strong_api_token_accepted(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_SECURITY__API_TOKEN", _STRONG_TOKEN)

    settings = load_settings()

    assert settings.security.api_token is not None
    assert settings.security.api_token.get_secret_value() == _STRONG_TOKEN


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.53"])
def test_loopback_hosts_with_auth_disabled_boot_fine(
    host: str, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_refuse_unsafe_bind`'s check short-circuits on `security.enabled` — `
    _is_loopback` is only ever reached when auth is disabled, so this is the path
    that actually exercises every loopback spelling (not
    `test_loopback_hosts_without_token_load` above, which runs with the `True`
    default and never reaches it at all).
    """
    monkeypatch.setenv("ANSINA_SECURITY__ENABLED", "false")
    monkeypatch.setenv("ANSINA_SERVER__HOST", host)

    settings = load_settings()

    assert settings.security.enabled is False
    assert settings.server.host == host


def test_non_loopback_bind_with_auth_disabled_refuses_to_start(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_SECURITY__ENABLED", "false")
    monkeypatch.setenv("ANSINA_SERVER__HOST", "0.0.0.0")

    with pytest.raises(ConfigError) as exc_info:
        load_settings()

    message = str(exc_info.value)
    assert "server.host" in message
    assert "ANSINA_SECURITY__ENABLED" in message


@pytest.mark.parametrize("host", ["0.0.0.0", "", "::", "not-an-ip-or-localhost"])
def test_non_loopback_hosts_boot_fine_with_default_settings(
    host: str, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike the pre-#24 model, `api_token` being unset no longer means "no auth" —
    Ansina generates its own bootstrap token (`ansina.auth.bootstrap`) — so a
    non-loopback bind with `security.enabled` left at its `True` default and no
    explicit override is safe and must not be refused.
    """
    monkeypatch.setenv("ANSINA_SERVER__HOST", host)

    settings = load_settings()

    assert settings.security.enabled is True
    assert settings.security.api_token is None


def test_non_loopback_bind_with_token_loads(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_SERVER__HOST", "0.0.0.0")
    monkeypatch.setenv("ANSINA_SECURITY__API_TOKEN", _STRONG_TOKEN)

    settings = load_settings()

    assert settings.server.host == "0.0.0.0"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.53"])
def test_loopback_hosts_without_token_load(
    host: str, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_SERVER__HOST", host)

    settings = load_settings()

    assert settings.server.host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "", "::", "not-an-ip-or-localhost"])
def test_non_loopback_hosts_with_auth_disabled_refuse_to_start(
    host: str, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_SECURITY__ENABLED", "false")
    monkeypatch.setenv("ANSINA_SERVER__HOST", host)

    with pytest.raises(ConfigError):
        load_settings()


def test_top_level_section_type_error_reports_top_level_key(
    clean_env: None, tmp_cwd: Path
) -> None:
    """A bad top-level section (not just a bad leaf field) still reads back cleanly."""
    (tmp_cwd / "ansina.toml").write_text('server = "not-a-table"\n', encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_settings()

    assert 'top-level key "server"' in str(exc_info.value)


def test_settings_is_frozen(clean_env: None, tmp_cwd: Path) -> None:
    settings = load_settings()

    with pytest.raises(ValidationError, match="frozen"):
        settings.server = settings.server


def test_database_path_expands_user_home(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_DATABASE__PATH", "~/ansina-data/ansina.db")

    settings = load_settings()

    assert settings.database.path == Path.home() / "ansina-data" / "ansina.db"


def test_database_path_resolves_relative_to_cwd(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_DATABASE__PATH", "nested/ansina.db")

    settings = load_settings()

    assert settings.database.path == tmp_cwd / "nested" / "ansina.db"


def test_database_path_absolute_is_unchanged(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    absolute = tmp_cwd / "elsewhere" / "ansina.db"
    monkeypatch.setenv("ANSINA_DATABASE__PATH", str(absolute))

    settings = load_settings()

    assert settings.database.path == absolute


def test_heart_context_tokens_above_ceiling_rejected(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """8192 is the blueprint's hard ceiling (issue #10) — config must reject more."""
    monkeypatch.setenv("ANSINA_HEART__CONTEXT_TOKENS", "8193")

    with pytest.raises(ConfigError) as exc_info:
        load_settings()

    assert "heart.context_tokens" in str(exc_info.value)


def test_heart_model_path_expands_user_home(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_HEART__MODEL_PATH", "~/models/heart")

    settings = load_settings()

    assert settings.heart.model_path == Path.home() / "models" / "heart"


def test_heart_cache_dir_resolves_relative_to_cwd(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_HEART__CACHE_DIR", "nested/heart-cache")

    settings = load_settings()

    assert settings.heart.cache_dir == tmp_cwd / "nested" / "heart-cache"


def test_heart_enabled_via_env(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_HEART__ENABLED", "true")

    settings = load_settings()

    assert settings.heart.enabled is True


def test_heart_runtime_rejects_unknown_value(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only `"auto"`/`"mlx"` are valid — `"llama_cpp"` isn't a member yet (issue #10
    deferred that adapter to a follow-up issue), so it must be rejected, not silently
    accepted as a no-op.
    """
    monkeypatch.setenv("ANSINA_HEART__RUNTIME", "llama_cpp")

    with pytest.raises(ConfigError) as exc_info:
        load_settings()

    assert "heart.runtime" in str(exc_info.value)


def test_brain_settings_via_env(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_BRAIN__ENABLED", "true")
    monkeypatch.setenv("ANSINA_BRAIN__BASE_URL", "https://openrouter.example/v1")
    monkeypatch.setenv("ANSINA_BRAIN__MODEL", "some-model")
    monkeypatch.setenv("ANSINA_BRAIN__API_KEY", "s3cr3t-brain-key-0123")
    monkeypatch.setenv("ANSINA_BRAIN__MAX_RETRIES", "5")

    settings = load_settings()

    assert settings.brain.enabled is True
    assert settings.brain.base_url == "https://openrouter.example/v1"
    assert settings.brain.model == "some-model"
    assert settings.brain.api_key is not None
    assert settings.brain.api_key.get_secret_value() == "s3cr3t-brain-key-0123"
    assert settings.brain.max_retries == 5
    assert "s3cr3t-brain-key-0123" not in repr(settings)


def test_brain_api_key_in_toml_file_rejected(clean_env: None, tmp_cwd: Path) -> None:
    (tmp_cwd / "ansina.toml").write_text(
        '[brain]\napi_key = "leaked-brain-key"\n', encoding="utf-8"
    )

    with pytest.raises(ConfigError) as exc_info:
        load_settings()

    message = str(exc_info.value)
    assert "ANSINA_BRAIN__API_KEY" in message
    assert "leaked-brain-key" not in message


def test_brain_max_retries_rejects_negative(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_BRAIN__MAX_RETRIES", "-1")

    with pytest.raises(ConfigError) as exc_info:
        load_settings()

    assert "brain.max_retries" in str(exc_info.value)


def test_tick_settings_via_env(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_HEART__TICK__ENABLED", "false")
    monkeypatch.setenv("ANSINA_HEART__TICK__INTERVAL_SECONDS", "10")
    monkeypatch.setenv("ANSINA_HEART__TICK__JITTER_SECONDS", "0")

    settings = load_settings()

    assert settings.heart.tick.enabled is False
    assert settings.heart.tick.interval_seconds == 10.0
    assert settings.heart.tick.jitter_seconds == 0.0


def test_tick_settings_via_toml(clean_env: None, tmp_cwd: Path) -> None:
    (tmp_cwd / "ansina.toml").write_text(
        "[heart.tick]\ninterval_seconds = 5.0\n", encoding="utf-8"
    )

    settings = load_settings()

    assert settings.heart.tick.interval_seconds == 5.0
    assert settings.heart.tick.jitter_seconds == 3.0  # untouched key keeps its default


def test_tick_interval_seconds_must_be_positive(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_HEART__TICK__INTERVAL_SECONDS", "0")

    with pytest.raises(ConfigError) as exc_info:
        load_settings()

    assert "heart.tick.interval_seconds" in str(exc_info.value)


def test_tick_jitter_seconds_rejects_negative(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_HEART__TICK__JITTER_SECONDS", "-1")

    with pytest.raises(ConfigError) as exc_info:
        load_settings()

    assert "heart.tick.jitter_seconds" in str(exc_info.value)


def test_unwrap_model_returns_none_for_a_non_model_annotation() -> None:
    """`int` contains no nested `BaseModel` anywhere in its args — the recursive walk
    bottoms out and reports "nothing found" rather than a model.
    """
    assert _unwrap_model(int) is None


def test_unwrap_model_finds_a_model_nested_inside_a_union() -> None:
    """None of Ansina's own fields wrap a `BaseModel` in a `Union` (only `SecretStr`
    fields do that, via `SecretStr | None`), so nothing in `load_settings` exercises
    `_unwrap_model`'s recursive branch — this drives it directly with `X | None`.
    """
    assert _unwrap_model(ServerSettings | None) is ServerSettings


def test_env_var_with_unparseable_value_for_nested_model_raises_config_error(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting the *whole-section* env var (not a `__`-nested leaf) to a non-JSON
    value makes pydantic-settings itself raise `SettingsError` while parsing the env
    source — a different failure mode than a `ValidationError` on a leaf field, and
    `load_settings` must wrap it in the same `ConfigError` report either way.
    """
    monkeypatch.setenv("ANSINA_SERVER", "not-json")

    with pytest.raises(ConfigError) as exc_info:
        load_settings()

    assert "server" in str(exc_info.value)
