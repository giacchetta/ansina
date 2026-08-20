from pathlib import Path

import pytest
from pydantic import ValidationError

from ansina.config import ConfigError, load_settings


def test_defaults_only(clean_env: None, tmp_cwd: Path) -> None:
    """No file, no env -> every documented default."""
    settings = load_settings()

    assert settings.server.host == "127.0.0.1"
    assert settings.server.port == 8000
    assert settings.logging.level == "INFO"
    assert settings.database.path == tmp_cwd / "ansina.db"
    assert settings.security.api_token is None


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


def test_secret_via_env_loads_and_never_appears_in_text(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_SECURITY__API_TOKEN", "s3cr3t-value-0123")

    settings = load_settings()

    assert settings.security.api_token is not None
    assert settings.security.api_token.get_secret_value() == "s3cr3t-value-0123"
    assert "s3cr3t-value-0123" not in repr(settings)
    assert "s3cr3t-value-0123" not in str(settings)


def test_secret_not_leaked_in_error_report_for_sibling_failure(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_SECURITY__API_TOKEN", "s3cr3t-value-0123")
    toml_path = tmp_cwd / "ansina.toml"
    toml_path.write_text('[server]\nport = "eighty"\n', encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_settings()

    assert "s3cr3t-value-0123" not in str(exc_info.value)


def test_short_api_token_rejected(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_SECURITY__API_TOKEN", "too-short")

    with pytest.raises(ConfigError) as exc_info:
        load_settings()

    message = str(exc_info.value)
    assert "security.api_token" in message
    assert "too-short" not in message


def test_non_loopback_bind_without_token_refuses_to_start(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_SERVER__HOST", "0.0.0.0")

    with pytest.raises(ConfigError) as exc_info:
        load_settings()

    message = str(exc_info.value)
    assert "server.host" in message
    assert "ANSINA_SECURITY__API_TOKEN" in message


def test_non_loopback_bind_with_token_loads(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_SERVER__HOST", "0.0.0.0")
    monkeypatch.setenv("ANSINA_SECURITY__API_TOKEN", "s3cr3t-value-0123")

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
def test_non_loopback_hosts_without_token_refuse_to_start(
    host: str, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
