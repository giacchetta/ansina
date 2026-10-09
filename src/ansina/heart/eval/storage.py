"""S3-compatible object store for the Heart bench/soak report corpus. See issue #59.

`docs/heart/bench/`/`docs/heart/soak/` are gitignored stopgaps (issues #58/#56) — a
report's raw per-fixture model output has no size ceiling, and the never-overwrite
auto-suffix discipline both directories already follow means the corpus can only
grow. This module gives that corpus a real, shared home: a structural `ReportStorage`
`Protocol` (the same port discipline as `HeartRuntime`/`BrainProvider`/
`OidcHttpClient`/`heart.eval.provenance.GitRunner`) plus `S3CompatibleStorage`, the one
adapter — "S3-compatible," not "AWS," so a configurable `endpoint_url` lets Cloudflare
R2 (the M7 lab/acceptance target), MinIO, Backblaze B2, Wasabi, or GCP Cloud Storage's
S3/XML interop stand in for AWS S3 itself, mirroring how `[brain] base_url` already
lets any OpenAI-compatible endpoint stand in for OpenAI. Support matrix: R2 is
verified on real hardware during M7's own acceptance run; the others are supported by
configuration but not independently verified here — GCS interop in particular is
"verify, don't assume," since its multipart-upload behaviour differs from AWS's and
this module never needs multipart (every bench/soak file is small).

`boto3` (`ansina[s3]`, an *optional* extra — dev-tooling-only, never installed in
either CI leg, the same optionality `ansina[mlx]` already has) is imported **inside a
function body**, never at module scope, so `ansina.heart.eval` stays importable
without the extra — exactly how `heart/models.py` treats `huggingface_hub`.

Bucket key layout (hive-partitioned, documented here since this module owns the
contract both `heart/eval/__main__.py`'s upload hook and `heart/eval/publish.py`'s
backlog migration build on):

    s3://<bucket>/<prefix>/kind=bench/dt=<date>/<stem>.{md,json}
    s3://<bucket>/<prefix>/kind=soak/dt=<date>/<run_id>/{samples.jsonl,run.log,
        journal.json,soak-<date>.md}
    s3://<bucket>/<prefix>/_contract/corpus-schema-v<N>.json

(The `_contract/` schema document itself is issue #63's own concern, published by that
issue's code, not this module's — named here only so the layout reads as one piece.
Likewise `kind=telemetry`/`kind=runlog`, issue #61/#62's partitions, are documented
here purely so the layout is consistent end-to-end; nothing in this module writes
them.)

Every key this module hands to or returns from `ReportStorage` is **prefix-free** —
`key_prefix` is applied/stripped entirely inside `S3CompatibleStorage`, so no caller
ever has to know the configured prefix exists.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, Protocol

from ansina.config.settings import S3Settings
from ansina.errors import TelemetryError
from ansina.logging import get_logger

logger = get_logger(__name__)


class ObjectStoreUnavailableError(TelemetryError):
    """`[telemetry.s3] enabled = true` but the configuration can't be used (no
    `bucket`, or either credential missing) — or the `boto3` package isn't
    installed. Raised by `build_report_storage`/`S3CompatibleStorage`'s own client
    construction, never by an upload itself (see `ObjectStoreUploadError`).
    """

    code: ClassVar[str] = "ansina.telemetry.object_store_unavailable"


class ObjectStoreUploadError(TelemetryError):
    """A real upload/list call against the object store failed (network, auth,
    missing bucket, ...). `boto3`/`botocore`'s own exception types aren't ours to
    name, so every failure is caught broadly and wrapped here — the same reasoning
    `heart.models._download_from_hub` already applies to `huggingface_hub`.
    """

    code: ClassVar[str] = "ansina.telemetry.object_store_upload_failed"


class ReportStorage(Protocol):
    """The port. Structural, like `HeartRuntime`/`BrainProvider` — see module
    docstring. `existing_keys` is a deliberate widening beyond issue #59's own
    written scope (a single `upload` method): the issue's own AC requires
    `make heart-bench-publish` to be a *verified* no-op on a second run, and the
    `heart/eval/__main__.py` upload hook's own never-overwrite requirement (mirroring
    `scripts/remote-heart.sh`'s local auto-suffix) both need a read side. One
    paginated list call amortizes far better than a `head_object` per candidate file.
    """

    def upload(self, local_path: Path, key: str) -> None:
        """Uploads `local_path`'s bytes to `key` (prefix-free — see module
        docstring). Raises `ObjectStoreUploadError` on any failure; never silently
        drops one.
        """
        ...

    def existing_keys(self, prefix: str) -> frozenset[str]:
        """Every object key (prefix-free) already in the bucket under `prefix`.
        Raises `ObjectStoreUploadError` on any failure — listing is just as capable
        of failing (network, auth) as uploading, and callers already handle that
        exception type for `upload`.
        """
        ...


def bench_key(filename: str) -> str:
    """`docs/heart/bench/<filename>` -> `kind=bench/dt=<date>/<filename>`, where
    `<date>` is `filename`'s own leading `YYYY-MM-DD` (every bench report is named
    `<date>-<model>-<variant>[-suffix].{md,json}` by `heart/eval/__main__.py`).
    """
    return f"kind=bench/dt={filename[:10]}/{filename}"


def soak_key(run_id: str, filename: str) -> str:
    """`docs/heart/soak/...` -> `kind=soak/dt=<date>/<run_id>/<filename>`, where
    `<date>` is `run_id`'s own leading `YYYY-MM-DD` (a soak `run_id` is `<date>` or
    `<date>-<n>` on a same-day collision — see `heart_soak_report.py`'s own
    `_unique_stem`).
    """
    return f"kind=soak/dt={run_id[:10]}/{run_id}/{filename}"


def next_available_stem(stem: str, taken: frozenset[str]) -> str:
    """`stem`, or `stem-2`/`stem-3`/... — the first not already a key in `taken` once
    passed through `key_fn`. Mirrors `scripts/remote-heart.sh`'s local auto-suffix
    discipline (never overwrite a report), extended to the bucket.

    `taken` holds full (prefix-free) keys already in the bucket for the kind in
    question; a candidate stem collides when *either* of its two file suffixes
    (`.md`/`.json`) would land on an existing key — checked via `bench_key`, the only
    caller of this function (the soak side is never suffixed; see
    `heart/eval/publish.py`'s module docstring for why).
    """
    if bench_key(f"{stem}.md") not in taken and bench_key(f"{stem}.json") not in taken:
        return stem
    n = 2
    while True:
        candidate = f"{stem}-{n}"
        if (
            bench_key(f"{candidate}.md") not in taken
            and bench_key(f"{candidate}.json") not in taken
        ):
            return candidate
        n += 1


# `(s3_settings) -> a boto3 S3 client`. Injected by tests so the unit suite never
# needs a real `boto3` installation or network access; the default implementation,
# `_build_client`, imports `boto3` inside its own body (see module docstring).
ClientFactory = Callable[[S3Settings], object]


def _build_client(s3: S3Settings) -> object:
    try:
        import boto3
    except ImportError as exc:
        raise ObjectStoreUnavailableError(
            "telemetry.s3.enabled is true but boto3 is not installed — install it "
            "via `uv sync --extra s3`"
        ) from exc

    # Narrows `SecretStr | None` to `SecretStr` for mypy — `build_report_storage`
    # (this function's only caller) already refuses to call it unless both are set.
    assert s3.access_key_id is not None
    assert s3.secret_access_key is not None
    kwargs: dict[str, object] = {
        "region_name": s3.region,
        "aws_access_key_id": s3.access_key_id.get_secret_value(),
        "aws_secret_access_key": s3.secret_access_key.get_secret_value(),
    }
    if s3.endpoint_url:
        kwargs["endpoint_url"] = s3.endpoint_url
    return boto3.client("s3", **kwargs)


class S3CompatibleStorage:
    """The one `ReportStorage` adapter. `client` is a boto3 S3 client (or a test
    fake satisfying the same `put_object`/`get_paginator("list_objects_v2")`
    surface) — never constructed here directly; see `build_report_storage`.
    """

    def __init__(self, *, bucket: str, key_prefix: str, client: object) -> None:
        self._bucket = bucket
        # Normalized once, to exactly one trailing slash (or none at all) — so
        # prepending (`_full_key`) and stripping (`existing_keys`) always agree on
        # the same literal string, regardless of how many trailing slashes the
        # configured `key_prefix` itself had.
        self._prefix_root = f"{key_prefix.rstrip('/')}/" if key_prefix else ""
        self._client = client

    def _full_key(self, key: str) -> str:
        return f"{self._prefix_root}{key}"

    def upload(self, local_path: Path, key: str) -> None:
        full_key = self._full_key(key)
        try:
            with local_path.open("rb") as handle:
                self._client.put_object(  # type: ignore[attr-defined]
                    Bucket=self._bucket, Key=full_key, Body=handle.read()
                )
        except Exception as exc:  # boto3/botocore's own exception types (ClientError,
            # EndpointConnectionError, NoCredentialsError, ...) aren't ours to name —
            # see `ObjectStoreUploadError`'s own docstring.
            raise ObjectStoreUploadError(
                f"failed to upload {local_path} to s3://{self._bucket}/{full_key}: "
                f"{exc}"
            ) from exc
        logger.info(
            "uploaded report to object store",
            extra={"bucket": self._bucket, "key": full_key},
        )

    def existing_keys(self, prefix: str) -> frozenset[str]:
        full_prefix = self._full_key(prefix)
        try:
            paginator = self._client.get_paginator(  # type: ignore[attr-defined]
                "list_objects_v2"
            )
            keys: set[str] = set()
            for page in paginator.paginate(Bucket=self._bucket, Prefix=full_prefix):
                for obj in page.get("Contents", []):
                    full_key = obj["Key"]
                    keys.add(full_key[len(self._prefix_root) :])
            return frozenset(keys)
        except Exception as exc:
            raise ObjectStoreUploadError(
                f"failed to list s3://{self._bucket}/{full_prefix}: {exc}"
            ) from exc


def build_report_storage(
    s3: S3Settings, *, client_factory: ClientFactory | None = None
) -> ReportStorage | None:
    """`None` when `[telemetry.s3] enabled = false` (the default) — same "no-op when
    off" shape `build_heart_runtime`/`build_brain_provider`/`build_oidc_login_service`
    already use. Raises `ObjectStoreUnavailableError` when enabled but `bucket` or
    either credential is missing/empty, or `boto3` isn't installed.

    `client_factory` is the test seam (mirrors `heart.models.resolve_model`'s own
    `downloader` parameter) — defaults to `_build_client`.
    """
    if not s3.enabled:
        return None

    missing = [
        name
        for name, value in (
            ("bucket", s3.bucket),
            ("access_key_id", s3.access_key_id),
            ("secret_access_key", s3.secret_access_key),
        )
        if not value
    ]
    if missing:
        fields = ", ".join(f"telemetry.s3.{name}" for name in missing)
        raise ObjectStoreUnavailableError(
            f"telemetry.s3.enabled is true but {fields} "
            f"{'is' if len(missing) == 1 else 'are'} not set"
        )

    factory = client_factory if client_factory is not None else _build_client
    client = factory(s3)
    return S3CompatibleStorage(
        bucket=s3.bucket, key_prefix=s3.key_prefix, client=client
    )
