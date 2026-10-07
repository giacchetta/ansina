from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from ansina.config.settings import DevSettings, S3Settings, Settings, TelemetrySettings
from ansina.dev.sidecar import SupervisedProcess
from ansina.dev.vector import (
    ENV_AWS_ACCESS_KEY_ID,
    ENV_AWS_SECRET_ACCESS_KEY,
    ENV_S3_BUCKET,
    ENV_S3_ENDPOINT_URL,
    ENV_S3_KEY_PREFIX,
    ENV_S3_REGION,
    ENV_SPOOL_DIR,
    ENV_VECTOR_DATA_DIR,
    _normalize_key_prefix,
    build_vector_env,
    build_vector_sidecar,
    preflight,
)


def _settings(
    *,
    telemetry_enabled: bool = True,
    s3_enabled: bool = True,
    bucket: str = "my-bucket",
    access_key_id: str | None = "AKIA",
    secret_access_key: str | None = "s3cr3t",
    vector_binary: str = "vector",
    vector_config: Path | None = None,
    spool_dir: Path | None = None,
    key_prefix: str = "",
) -> Settings:
    s3 = S3Settings(
        enabled=s3_enabled,
        bucket=bucket,
        endpoint_url="https://abc123.r2.cloudflarestorage.com",
        region="auto",
        key_prefix=key_prefix,
        access_key_id=SecretStr(access_key_id) if access_key_id else None,
        secret_access_key=SecretStr(secret_access_key) if secret_access_key else None,
    )
    telemetry = TelemetrySettings(
        enabled=telemetry_enabled,
        spool_dir=spool_dir if spool_dir is not None else Path("/tmp/ansina-telemetry"),
        s3=s3,
    )
    dev = DevSettings(
        vector_binary=vector_binary,
        vector_config=vector_config
        if vector_config is not None
        else Path("/nonexistent/vector.toml"),
    )
    return Settings(telemetry=telemetry, dev=dev)


# --- build_vector_env ------------------------------------------------------------


def test_build_vector_env_includes_only_the_allow_listed_names() -> None:
    settings = _settings(key_prefix="prod")
    base_env = {
        "PATH": "/usr/bin",
        "HOME": "/home/ansina",
        "TMPDIR": "/tmp",
        "ANSINA_SECURITY__API_TOKEN": "super-secret-token",
        "ANSINA_BRAIN__API_KEY": "brain-secret",
        "UNRELATED": "noise",
    }

    env = build_vector_env(settings, base_env=base_env)

    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/ansina"
    assert env["TMPDIR"] == "/tmp"
    assert "ANSINA_SECURITY__API_TOKEN" not in env
    assert "ANSINA_BRAIN__API_KEY" not in env
    assert "UNRELATED" not in env


def test_build_vector_env_carries_telemetry_and_s3_fields() -> None:
    settings = _settings(key_prefix="prod", spool_dir=Path("/var/spool/telemetry"))

    env = build_vector_env(settings, base_env={})

    assert env[ENV_SPOOL_DIR] == "/var/spool/telemetry"
    assert env[ENV_VECTOR_DATA_DIR] == "/var/spool/telemetry/vector"
    assert env[ENV_S3_BUCKET] == "my-bucket"
    assert env[ENV_S3_ENDPOINT_URL] == "https://abc123.r2.cloudflarestorage.com"
    assert env[ENV_S3_REGION] == "auto"
    assert env[ENV_S3_KEY_PREFIX] == "prod/"
    assert env[ENV_AWS_ACCESS_KEY_ID] == "AKIA"
    assert env[ENV_AWS_SECRET_ACCESS_KEY] == "s3cr3t"


def test_build_vector_env_omits_aws_credentials_when_unset() -> None:
    settings = _settings(access_key_id=None, secret_access_key=None)

    env = build_vector_env(settings, base_env={})

    assert ENV_AWS_ACCESS_KEY_ID not in env
    assert ENV_AWS_SECRET_ACCESS_KEY not in env


def test_build_vector_env_defaults_to_the_real_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/custom/path")
    settings = _settings()

    env = build_vector_env(settings)

    assert env["PATH"] == "/custom/path"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", ""), ("prod", "prod/"), ("prod/", "prod/"), ("prod//", "prod/")],
)
def test_normalize_key_prefix(raw: str, expected: str) -> None:
    assert _normalize_key_prefix(raw) == expected


# --- preflight ---------------------------------------------------------------


def test_preflight_skips_when_telemetry_disabled(
    captured_logs: Any,
) -> None:
    settings = _settings(telemetry_enabled=False)

    result = preflight(settings)

    assert result.vector_path is None
    assert result.skip_reason is not None
    assert "telemetry.enabled is false" in result.skip_reason
    lines = captured_logs()
    assert any(
        line["level"] == "WARNING"
        and "telemetry.enabled is false" in line["extra"]["reason"]
        for line in lines
    )


def test_preflight_skips_when_s3_disabled() -> None:
    settings = _settings(s3_enabled=False)

    result = preflight(settings)

    assert result.vector_path is None
    assert result.skip_reason is not None
    assert "not fully configured" in result.skip_reason


@pytest.mark.parametrize(
    "kwargs",
    [
        {"bucket": ""},
        {"access_key_id": None},
        {"secret_access_key": None},
    ],
)
def test_preflight_skips_when_s3_missing_a_required_field(
    kwargs: dict[str, Any],
) -> None:
    settings = _settings(**kwargs)

    result = preflight(settings)

    assert result.vector_path is None
    assert result.skip_reason is not None
    assert "not fully configured" in result.skip_reason


def test_preflight_skips_when_vector_binary_not_on_path() -> None:
    settings = _settings(vector_binary="definitely-not-a-real-binary-xyz")

    result = preflight(settings)

    assert result.vector_path is None
    assert result.skip_reason is not None
    assert "not found on PATH" in result.skip_reason


def test_preflight_skips_when_vector_config_does_not_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/vector")
    settings = _settings(vector_config=Path("/nonexistent/vector.toml"))

    result = preflight(settings)

    assert result.vector_path is None
    assert result.skip_reason is not None
    assert "does not exist" in result.skip_reason


def test_preflight_skips_when_validate_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/vector")
    config = tmp_path / "vector.toml"
    config.write_text("# not valid\n")
    settings = _settings(vector_config=config)

    def _runner(
        args: Any,
        *,
        env: Any,
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args, returncode=1, stdout="", stderr="bad config"
        )

    result = preflight(settings, runner=_runner)

    assert result.vector_path is None
    assert result.skip_reason is not None
    assert "rejected" in result.skip_reason


def test_preflight_skips_when_validate_raises_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/vector")
    config = tmp_path / "vector.toml"
    config.write_text("# valid enough\n")
    settings = _settings(vector_config=config)

    def _runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("vector: command not found")

    result = preflight(settings, runner=_runner)

    assert result.vector_path is None
    assert result.skip_reason is not None
    assert "failed to run" in result.skip_reason


def test_preflight_skips_when_validate_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/vector")
    config = tmp_path / "vector.toml"
    config.write_text("# valid enough\n")
    settings = _settings(vector_config=config)

    def _runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="vector", timeout=30.0)

    result = preflight(settings, runner=_runner)

    assert result.vector_path is None
    assert result.skip_reason is not None
    assert "failed to run" in result.skip_reason


def test_preflight_creates_the_vector_data_dir_before_validating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`vector validate` itself checks that `data_dir` exists (empirically
    confirmed against a real `vector` binary) — `preflight()` must create it
    *before* that call runs, not only after a successful preflight the way
    `build_vector_sidecar()` once did, or validation of an otherwise-correct
    config fails on a chicken-and-egg directory-not-found error.
    """
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/vector")
    config = tmp_path / "vector.toml"
    config.write_text("# valid\n")
    spool_dir = tmp_path / "spool"
    settings = _settings(vector_config=config, spool_dir=spool_dir)
    seen_data_dir_exists: list[bool] = []

    def _runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen_data_dir_exists.append((spool_dir / "vector").is_dir())
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    result = preflight(settings, runner=_runner)

    assert result.skip_reason is None
    assert seen_data_dir_exists == [True]


def test_preflight_rejection_reads_the_output_vector_actually_writes_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empirically, a real `vector validate` writes its failure diagnostics to
    stdout, not stderr — the rejection message must surface what was actually
    printed, not silently read an empty string from the wrong stream.
    """
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/vector")
    config = tmp_path / "vector.toml"
    config.write_text("# not valid\n")
    settings = _settings(vector_config=config)

    def _runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args, returncode=78, stdout='x data_dir "..." does not exist', stderr=""
        )

    result = preflight(settings, runner=_runner)

    assert result.vector_path is None
    assert result.skip_reason is not None
    assert "data_dir" in result.skip_reason


def test_preflight_passes_every_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/vector")
    config = tmp_path / "vector.toml"
    config.write_text("# valid\n")
    settings = _settings(vector_config=config)
    captured: dict[str, Any] = {}

    def _runner(
        args: Any,
        *,
        env: Any,
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured["env"] = env
        captured["timeout"] = timeout
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    result = preflight(settings, runner=_runner)

    assert result.vector_path == "/usr/bin/vector"
    assert result.skip_reason is None
    assert captured["args"] == [
        "/usr/bin/vector",
        "validate",
        "--skip-healthchecks",
        str(config),
    ]
    assert captured["timeout"] == settings.dev.validate_timeout_seconds


# --- build_vector_sidecar ------------------------------------------------------


def test_build_vector_sidecar_returns_none_on_failed_preflight() -> None:
    settings = _settings(telemetry_enabled=False)

    assert build_vector_sidecar(settings) is None


def test_build_vector_sidecar_builds_a_supervised_process_and_creates_the_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/vector")
    config = tmp_path / "vector.toml"
    config.write_text("# valid\n")
    spool_dir = tmp_path / "spool"
    settings = _settings(vector_config=config, spool_dir=spool_dir)

    def _runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    sidecar = build_vector_sidecar(settings, runner=_runner)

    assert isinstance(sidecar, SupervisedProcess)
    assert (spool_dir / "vector").is_dir()
    assert sidecar.argv == ("/usr/bin/vector", "--config", str(config))
