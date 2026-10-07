"""The Vector sidecar: environment-only credential handoff, logged-skip preflight,
and the one occupant `ansina.dev.sidecar.SupervisedProcess` supervises. See issue
#62.

Dev Mode is the one place this codebase deliberately narrows the "the daemon
itself never shells out" invariant `heart.eval.provenance`'s own module docstring
documents (and `ansina.telemetry.sampler`'s docstring cites as the reason `rss_kib`
reads `getrusage` rather than shelling out to `ps`): subprocess use here is
confined to `ansina.dev`, reached only once, at boot, from `__main__.py` before
uvicorn binds a port — never from `create_app()`, its lifespan, a route, or any
periodic loop. `TelemetrySampler`'s own per-sample reasoning is unaffected.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ansina.config.settings import Settings
from ansina.dev.sidecar import SupervisedProcess
from ansina.logging import get_logger

logger = get_logger(__name__)

_VECTOR_DATA_DIR_NAME = "vector"

# **Pin `vector` to 0.45.x** (empirically verified against a real Cloudflare R2
# bucket) — not a preference, a load-bearing compatibility requirement found
# during this issue's own implementation, documented here rather than discovered
# later by whoever next installs a newer release:
#
# Every Vector release after ~0.45 bundles an `aws-sdk-rust` whose `aws_s3`
# sink/source client unconditionally adds a default `x-amz-checksum-*` request
# checksum (awslabs/aws-sdk-rust#1240) *on top of* Vector's own long-standing
# `Content-MD5` header on every `PutObject`. Plain AWS S3 tolerates both headers;
# Cloudflare R2 rejects the combination outright with
# `InvalidRequest: You can only specify one non-default checksum at a time`
# (reproduced verbatim against this project's own R2 bucket on `vector 0.59.0`).
# The documented cross-SDK env vars for this (`AWS_REQUEST_CHECKSUM_CALCULATION`/
# `AWS_RESPONSE_CHECKSUM_VALIDATION`) do **not** fix it — Vector's own client
# builder doesn't read them (confirmed by testing both, and independently by
# other reporters: vectordotdev/vector#23029). A fix exists
# (vectordotdev/vector#25803, restricting `request_checksum_calculation` to
# `WhenRequired` on the SDK client Vector itself builds) but is **not merged or
# released as of this writing** — `0.45.0` (the version this project tests
# against) simply predates the SDK bump that introduced the behavior.
# Re-evaluate this pin once #25803 ships in a release.
#
# A second consequence of that same older baseline: `0.45.0` has no
# `--dangerously-allow-env-var-interpolation` flag at all — `${VAR}` interpolation
# in `deploy/vector.toml` is its unconditional default behavior, not something
# this module has to request. Passing that flag to `0.45.0` is an unrecognized
# argument and a hard CLI error, so it is deliberately **not** part of either
# argv this module builds.

# Environment variable names `deploy/vector.toml` interpolates. Single-underscore,
# `ANSINA_TELEMETRY_*`/`AWS_*` — deliberately distinct from Ansina's own
# `__`-double-underscore-delimited settings schema (`ANSINA_TELEMETRY__SPOOL_DIR`,
# ...), so the two namespaces can never collide and a process-environment dump
# never mistakes one for the other.
ENV_SPOOL_DIR = "ANSINA_TELEMETRY_SPOOL_DIR"
ENV_VECTOR_DATA_DIR = "ANSINA_TELEMETRY_VECTOR_DATA_DIR"
ENV_S3_BUCKET = "ANSINA_TELEMETRY_S3_BUCKET"
ENV_S3_ENDPOINT_URL = "ANSINA_TELEMETRY_S3_ENDPOINT_URL"
ENV_S3_REGION = "ANSINA_TELEMETRY_S3_REGION"
ENV_S3_KEY_PREFIX = "ANSINA_TELEMETRY_S3_KEY_PREFIX"
ENV_AWS_ACCESS_KEY_ID = "AWS_ACCESS_KEY_ID"
ENV_AWS_SECRET_ACCESS_KEY = "AWS_SECRET_ACCESS_KEY"

# Inherited from the daemon's own process environment, unfiltered — everything else
# in `build_vector_env` is an explicit allow-list. Ansina's own secrets
# (`ANSINA_SECURITY__API_TOKEN`, `ANSINA_BRAIN__API_KEY`, the OIDC client secret,
# ...) are never in this tuple and so never reach the child's own
# `/proc/<pid>/environ`, regardless of what the daemon's own environment holds.
_INHERITED_ENV_NAMES = ("PATH", "HOME", "TMPDIR")


class ValidateRunner(Protocol):
    """The exact slice of `subprocess.run` `preflight()` calls — mirrors
    `heart.eval.provenance.GitRunner`'s own narrow-slice-of-the-stdlib shape, so the
    unit suite injects a fake with no real `vector` binary.
    """

    def __call__(
        self,
        args: Sequence[str],
        *,
        env: dict[str, str],
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]: ...


def _normalize_key_prefix(key_prefix: str) -> str:
    """Exactly one trailing slash, or empty — the identical expression
    `heart.eval.storage.S3CompatibleStorage.__init__` already uses for the same
    reason, so `deploy/vector.toml` can safely concatenate
    `"${ANSINA_TELEMETRY_S3_KEY_PREFIX}kind=telemetry/..."` without ever producing a
    doubled or missing slash.
    """
    return f"{key_prefix.rstrip('/')}/" if key_prefix else ""


def build_vector_env(
    settings: Settings, *, base_env: dict[str, str] | None = None
) -> dict[str, str]:
    """A narrow allow-list, not the daemon's own `os.environ` wholesale — only what
    `deploy/vector.toml` actually interpolates, plus the three inherited names
    Vector itself needs to run at all (`PATH` for its own subprocess/TLS lookups,
    `HOME`/`TMPDIR` for its own scratch use). `base_env` defaults to the real
    process environment; the unit suite passes a synthetic one so a test can assert
    exactly what is and isn't forwarded with no dependency on this machine's own
    environment.
    """
    base = os.environ if base_env is None else base_env
    telemetry = settings.telemetry
    env: dict[str, str] = {
        name: base[name] for name in _INHERITED_ENV_NAMES if name in base
    }
    env[ENV_SPOOL_DIR] = str(telemetry.spool_dir)
    env[ENV_VECTOR_DATA_DIR] = str(telemetry.spool_dir / _VECTOR_DATA_DIR_NAME)
    env[ENV_S3_BUCKET] = telemetry.s3.bucket
    env[ENV_S3_ENDPOINT_URL] = telemetry.s3.endpoint_url
    env[ENV_S3_REGION] = telemetry.s3.region
    env[ENV_S3_KEY_PREFIX] = _normalize_key_prefix(telemetry.s3.key_prefix)
    if telemetry.s3.access_key_id is not None:
        env[ENV_AWS_ACCESS_KEY_ID] = telemetry.s3.access_key_id.get_secret_value()
    if telemetry.s3.secret_access_key is not None:
        env[ENV_AWS_SECRET_ACCESS_KEY] = (
            telemetry.s3.secret_access_key.get_secret_value()
        )
    return env


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """Exactly one of the two fields is set, never both, never neither:
    `vector_path` on a clean pass through every check, `skip_reason` the moment any
    one check fails.
    """

    vector_path: str | None
    skip_reason: str | None


def _skip(reason: str) -> PreflightResult:
    logger.warning("dev mode: vector sidecar not started", extra={"reason": reason})
    return PreflightResult(vector_path=None, skip_reason=reason)


def preflight(
    settings: Settings, *, runner: ValidateRunner = subprocess.run
) -> PreflightResult:
    """Four logged-skip checks, cheapest first, each with its own specific log line
    (issue #62's own AC: "each failure reason is logged"). Never raises — "a
    shipper with nothing to ship is worse than no shipper" is the issue's own
    reasoning for its three named checks, and it extends to `[telemetry]` itself
    being off, added here as the first (and cheapest) check during planning.

    The `vector validate` call runs with real interpolation (not `--no-environment`
    — that would also hide a typo'd `${VAR}` name in `deploy/vector.toml` that
    doesn't match this module's own `ENV_*` constants, which is exactly the kind of
    mistake this check exists to catch) and `--skip-healthchecks` (no live network
    call against the bucket — reachability is what the post-spawn liveness check
    reports instead; `preflight()` must stay fast and local).
    """
    telemetry = settings.telemetry
    if not telemetry.enabled:
        return _skip("telemetry.enabled is false — nothing for Vector to tail")

    s3 = telemetry.s3
    missing = [
        name
        for name, value in (
            ("bucket", s3.bucket),
            ("access_key_id", s3.access_key_id),
            ("secret_access_key", s3.secret_access_key),
        )
        if not value
    ]
    if not s3.enabled or missing:
        return _skip(
            "telemetry.s3 is not fully configured for upload "
            f"(enabled={s3.enabled}, missing={missing or 'none'})"
        )

    vector_path = shutil.which(settings.dev.vector_binary)
    if vector_path is None:
        return _skip(f"vector binary {settings.dev.vector_binary!r} not found on PATH")

    vector_config = settings.dev.vector_config
    if not vector_config.is_file():
        return _skip(f"vector config {vector_config} does not exist")

    env = build_vector_env(settings)
    # `vector validate` itself checks that `data_dir` exists (empirically
    # confirmed — it's the one config field this check actually touches disk
    # for), so it must already exist by the time this call runs, not only once
    # `build_vector_sidecar` would otherwise create it *after* a successful
    # preflight. Idempotent and cheap; the one place this package creates it.
    Path(env[ENV_VECTOR_DATA_DIR]).mkdir(parents=True, exist_ok=True)
    try:
        completed = runner(
            [vector_path, "validate", "--skip-healthchecks", str(vector_config)],
            env=env,
            capture_output=True,
            text=True,
            timeout=settings.dev.validate_timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _skip(f"vector validate failed to run: {exc}")
    if completed.returncode != 0:
        # Empirically, `vector validate` writes its failure diagnostics to
        # stdout, not stderr — both are captured here regardless, since that
        # stream assignment isn't a documented contract this module should rely
        # on holding across every Vector version.
        output = (completed.stdout + completed.stderr).strip()
        return _skip(f"vector validate rejected {vector_config}: {output}")
    return PreflightResult(vector_path=vector_path, skip_reason=None)


def build_vector_sidecar(
    settings: Settings, *, runner: ValidateRunner = subprocess.run
) -> SupervisedProcess | None:
    """`None` on any failed preflight check — the same "no-op when off" shape
    `build_heart_runtime`/`build_brain_provider`/`build_oidc_login_service`/
    `build_report_storage` all use. `preflight()` itself already created the
    Vector data dir (its own `vector validate` call needs it to exist — see that
    function's comment), so there's nothing left to create here.
    """
    result = preflight(settings, runner=runner)
    if result.vector_path is None:
        return None

    env = build_vector_env(settings)
    dev = settings.dev
    return SupervisedProcess(
        argv=(result.vector_path, "--config", str(dev.vector_config)),
        env=env,
        liveness_delay_seconds=dev.liveness_delay_seconds,
        shutdown_timeout_seconds=dev.shutdown_timeout_seconds,
    )
