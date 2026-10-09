"""Dev Mode (issue #62) acceptance verifier — reads objects the Vector sidecar is
supposed to have uploaded directly out of the configured S3-compatible bucket,
never trusting Vector's own exit code or logs alone (the issue's own AC).

Run via `scripts/dev-mode-smoke.sh`, never ad hoc: it must inherit that script's
exact `ANSINA_TELEMETRY__*` environment (in particular the isolated
`_devmode-smoke/<host>-<ts>/` key prefix that run uploaded under), so this reads
`load_settings()` the same way every other Ansina entry point does — never
`os.getenv` directly, and never a second, parallel credential-resolution path.

Not `pytest`-collected or `mypy`-checked (same as every other `scripts/*.py` tool —
see `scripts/heart_journal_smoke_verify.py`), but `ruff check`/`ruff format`-clean.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from ansina.config import load_settings
from ansina.config.settings import S3Settings
from ansina.errors import AnsinaError

# The exact field set `ansina.telemetry.sampler.TelemetrySample` writes — mirrors
# the identical list `tests/e2e/test_server.py`'s own telemetry test already
# asserts against.
_SAMPLE_KEYS = (
    "t",
    "elapsed_s",
    "rss_kib",
    "ticks",
    "paused",
    "paused_reason",
    "last_decision",
    "last_duration_seconds",
    "failures_total",
    "consecutive_failures",
    "consecutive_overruns",
)


def _build_client(s3: S3Settings) -> Any:  # noqa: ANN401
    """Mirrors `ansina.heart.eval.storage._build_client` — duplicated rather than
    imported, since that's a private (`_`-prefixed) helper of another module and
    this is a separate dev-tooling entry point, the same "import `boto3` inside a
    function body, never at module scope" discipline either way.
    """
    import boto3

    assert s3.access_key_id is not None
    assert s3.secret_access_key is not None
    kwargs: dict[str, Any] = {
        "region_name": s3.region,
        "aws_access_key_id": s3.access_key_id.get_secret_value(),
        "aws_secret_access_key": s3.secret_access_key.get_secret_value(),
    }
    if s3.endpoint_url:
        kwargs["endpoint_url"] = s3.endpoint_url
    return boto3.client("s3", **kwargs)


def _normalize_prefix(prefix: str) -> str:
    """The identical expression `heart.eval.storage.S3CompatibleStorage.__init__`
    and `ansina.dev.vector._normalize_key_prefix` both already use.
    """
    return f"{prefix.rstrip('/')}/" if prefix else ""


def _find_one_key(client: Any, bucket: str, prefix: str) -> str | None:  # noqa: ANN401
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            assert isinstance(key, str)
            return key
    return None


def _validate_samples(body: bytes) -> str | None:
    """`None` on success, else a human-readable reason."""
    try:
        line = body.decode("utf-8").strip().splitlines()[0]
        sample = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError, IndexError) as exc:
        return f"not a JSON line: {exc}"
    missing = [key for key in _SAMPLE_KEYS if key not in sample]
    return f"missing keys: {missing}" if missing else None


def _validate_log(body: bytes) -> str | None:
    try:
        line = body.decode("utf-8").strip().splitlines()[0]
        record = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError, IndexError) as exc:
        return f"not a JSON line: {exc}"
    return None if "message" in record else "missing 'message' field"


def main() -> int:
    try:
        settings = load_settings()
    except AnsinaError as exc:
        print(f"FAIL: could not load settings: {exc}", file=sys.stderr)
        return 1

    s3 = settings.telemetry.s3
    if (
        not s3.enabled
        or not s3.bucket
        or s3.access_key_id is None
        or s3.secret_access_key is None
    ):
        print(
            "FAIL: telemetry.s3 is not fully configured in this environment",
            file=sys.stderr,
        )
        return 1

    prefix_root = _normalize_prefix(s3.key_prefix)
    client = _build_client(s3)

    ok = True
    for kind, validator in (
        ("telemetry", _validate_samples),
        ("runlog", _validate_log),
    ):
        prefix = f"{prefix_root}kind={kind}/"
        key = _find_one_key(client, s3.bucket, prefix)
        if key is None:
            print(
                f"FAIL: no object found under s3://{s3.bucket}/{prefix}",
                file=sys.stderr,
            )
            ok = False
            continue
        body = client.get_object(Bucket=s3.bucket, Key=key)["Body"].read()
        problem = validator(body)
        if problem is not None:
            print(f"FAIL: s3://{s3.bucket}/{key}: {problem}", file=sys.stderr)
            ok = False
            continue
        print(f"PASS: s3://{s3.bucket}/{key} ({len(body)} bytes)")

    print(f"\nSmoke prefix used: s3://{s3.bucket}/{prefix_root}", file=sys.stderr)
    print("Delete it once you're done inspecting it, e.g.:", file=sys.stderr)
    endpoint_flag = f" --endpoint-url {s3.endpoint_url}" if s3.endpoint_url else ""
    print(
        f"  aws s3 rm s3://{s3.bucket}/{prefix_root} --recursive{endpoint_flag}",
        file=sys.stderr,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
