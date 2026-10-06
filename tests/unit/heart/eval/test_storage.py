from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from ansina.config.settings import S3Settings
from ansina.heart.eval.storage import (
    ObjectStoreUnavailableError,
    ObjectStoreUploadError,
    S3CompatibleStorage,
    bench_key,
    build_report_storage,
    next_available_stem,
    soak_key,
)

# --- key builders -----------------------------------------------------------------


def test_bench_key_derives_date_from_filename() -> None:
    assert (
        bench_key("2026-09-29-gemma-4-e2b-it-4bit-strict.md")
        == "kind=bench/dt=2026-09-29/2026-09-29-gemma-4-e2b-it-4bit-strict.md"
    )


def test_soak_key_derives_date_from_run_id() -> None:
    assert (
        soak_key("2026-10-02", "samples.jsonl")
        == "kind=soak/dt=2026-10-02/2026-10-02/samples.jsonl"
    )


def test_soak_key_handles_a_suffixed_run_id() -> None:
    assert (
        soak_key("2026-10-02-2", "run.log")
        == "kind=soak/dt=2026-10-02/2026-10-02-2/run.log"
    )


def test_next_available_stem_returns_stem_unchanged_when_free() -> None:
    assert next_available_stem("2026-10-06-model-strict", frozenset()) == (
        "2026-10-06-model-strict"
    )


def test_next_available_stem_suffixes_on_md_collision() -> None:
    stem = "2026-10-06-model-strict"
    taken = frozenset({bench_key(f"{stem}.md")})

    assert next_available_stem(stem, taken) == f"{stem}-2"


def test_next_available_stem_suffixes_on_json_collision() -> None:
    stem = "2026-10-06-model-strict"
    taken = frozenset({bench_key(f"{stem}.json")})

    assert next_available_stem(stem, taken) == f"{stem}-2"


def test_next_available_stem_keeps_counting_past_multiple_collisions() -> None:
    stem = "2026-10-06-model-strict"
    taken = frozenset(
        {
            bench_key(f"{stem}.md"),
            bench_key(f"{stem}-2.md"),
            bench_key(f"{stem}-3.json"),
        }
    )

    assert next_available_stem(stem, taken) == f"{stem}-4"


# --- build_report_storage ----------------------------------------------------------


def test_build_report_storage_returns_none_when_disabled() -> None:
    assert build_report_storage(S3Settings(enabled=False)) is None


def test_build_report_storage_raises_when_bucket_missing() -> None:
    s3 = S3Settings(
        enabled=True,
        access_key_id=SecretStr("key"),
        secret_access_key=SecretStr("secret"),
    )

    with pytest.raises(ObjectStoreUnavailableError, match="telemetry\\.s3\\.bucket"):
        build_report_storage(s3)


def test_build_report_storage_raises_when_credentials_missing() -> None:
    s3 = S3Settings(enabled=True, bucket="my-bucket")

    with pytest.raises(ObjectStoreUnavailableError) as exc_info:
        build_report_storage(s3)

    message = str(exc_info.value)
    assert "telemetry.s3.access_key_id" in message
    assert "telemetry.s3.secret_access_key" in message


def test_build_report_storage_constructs_adapter_via_injected_factory() -> None:
    s3 = S3Settings(
        enabled=True,
        bucket="my-bucket",
        key_prefix="prod",
        access_key_id=SecretStr("key"),
        secret_access_key=SecretStr("secret"),
    )
    calls: list[S3Settings] = []

    def _factory(settings: S3Settings) -> object:
        calls.append(settings)
        return object()

    storage = build_report_storage(s3, client_factory=_factory)

    assert isinstance(storage, S3CompatibleStorage)
    assert calls == [s3]


def test_build_report_storage_uses_default_client_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercises `_build_client`'s real body (not just the injected-factory seam)
    by installing a stub `boto3` module — the same pattern
    `tests/unit/heart/test_models.py` uses for `huggingface_hub`.
    """
    s3 = S3Settings(
        enabled=True,
        bucket="my-bucket",
        endpoint_url="https://abc123.r2.cloudflarestorage.com",
        region="auto",
        access_key_id=SecretStr("AKIAEXAMPLE"),
        secret_access_key=SecretStr("s3cr3t"),
    )
    calls: list[dict[str, Any]] = []

    class _StubBoto3:
        @staticmethod
        def client(service: str, **kwargs: Any) -> str:
            calls.append({"service": service, **kwargs})
            return "a-boto3-client"

    monkeypatch.setitem(sys.modules, "boto3", _StubBoto3())

    storage = build_report_storage(s3)

    assert isinstance(storage, S3CompatibleStorage)
    assert calls == [
        {
            "service": "s3",
            "region_name": "auto",
            "aws_access_key_id": "AKIAEXAMPLE",
            "aws_secret_access_key": "s3cr3t",
            "endpoint_url": "https://abc123.r2.cloudflarestorage.com",
        }
    ]


def test_default_client_factory_omits_endpoint_url_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s3 = S3Settings(
        enabled=True,
        bucket="my-bucket",
        access_key_id=SecretStr("AKIAEXAMPLE"),
        secret_access_key=SecretStr("s3cr3t"),
    )
    calls: list[dict[str, Any]] = []

    class _StubBoto3:
        @staticmethod
        def client(service: str, **kwargs: Any) -> str:
            calls.append(kwargs)
            return "a-boto3-client"

    monkeypatch.setitem(sys.modules, "boto3", _StubBoto3())

    build_report_storage(s3)

    assert "endpoint_url" not in calls[0]


def test_build_report_storage_raises_unavailable_when_boto3_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s3 = S3Settings(
        enabled=True,
        bucket="my-bucket",
        access_key_id=SecretStr("key"),
        secret_access_key=SecretStr("secret"),
    )
    monkeypatch.setitem(sys.modules, "boto3", None)

    with pytest.raises(ObjectStoreUnavailableError, match="boto3"):
        build_report_storage(s3)


# --- S3CompatibleStorage ------------------------------------------------------------


class _FakePaginator:
    def __init__(self, keys: Iterable[str], *, fail: bool) -> None:
        self._keys = list(keys)
        self._fail = fail

    def paginate(
        self,
        *,
        Bucket: str,  # noqa: N803 — mirrors boto3's own real parameter name
        Prefix: str,  # noqa: N803 — mirrors boto3's own real parameter name
    ) -> Iterator[dict[str, list[dict[str, str]]]]:
        if self._fail:
            raise RuntimeError("list_objects_v2 boom")
        matching = [key for key in self._keys if key.startswith(Prefix)]
        # Two pages, to exercise the paginator loop itself rather than a single
        # one-shot page.
        mid = len(matching) // 2 or len(matching)
        pages = [matching[:mid], matching[mid:]]
        for page_keys in pages:
            if not page_keys and matching:
                continue
            yield {"Contents": [{"Key": key} for key in page_keys]}
        if not matching:
            yield {"Contents": []}


class _FakeClient:
    def __init__(
        self,
        *,
        existing_keys: Iterable[str] = (),
        fail_put: bool = False,
        fail_list: bool = False,
    ) -> None:
        self.existing_keys = set(existing_keys)
        self.put_calls: list[tuple[str, str, bytes]] = []
        self._fail_put = fail_put
        self._fail_list = fail_list

    def put_object(
        self,
        *,
        Bucket: str,  # noqa: N803 — mirrors boto3's own real parameter name
        Key: str,  # noqa: N803 — mirrors boto3's own real parameter name
        Body: bytes,  # noqa: N803 — mirrors boto3's own real parameter name
    ) -> None:
        if self._fail_put:
            raise RuntimeError("put_object boom")
        self.put_calls.append((Bucket, Key, Body))
        self.existing_keys.add(Key)

    def get_paginator(self, operation_name: str) -> _FakePaginator:
        assert operation_name == "list_objects_v2"
        return _FakePaginator(self.existing_keys, fail=self._fail_list)


def test_upload_writes_bytes_under_the_prefixed_key(tmp_path: Path) -> None:
    local_path = tmp_path / "report.md"
    local_path.write_text("hello report")
    client = _FakeClient()
    storage = S3CompatibleStorage(bucket="my-bucket", key_prefix="prod", client=client)

    storage.upload(local_path, "kind=bench/dt=2026-10-06/report.md")

    assert client.put_calls == [
        (
            "my-bucket",
            "prod/kind=bench/dt=2026-10-06/report.md",
            b"hello report",
        )
    ]


def test_upload_with_no_prefix_configured(tmp_path: Path) -> None:
    local_path = tmp_path / "report.md"
    local_path.write_text("hello")
    client = _FakeClient()
    storage = S3CompatibleStorage(bucket="my-bucket", key_prefix="", client=client)

    storage.upload(local_path, "kind=bench/dt=2026-10-06/report.md")

    assert client.put_calls == [
        ("my-bucket", "kind=bench/dt=2026-10-06/report.md", b"hello")
    ]


def test_upload_wraps_a_client_failure(tmp_path: Path) -> None:
    local_path = tmp_path / "report.md"
    local_path.write_text("hello")
    client = _FakeClient(fail_put=True)
    storage = S3CompatibleStorage(bucket="my-bucket", key_prefix="", client=client)

    with pytest.raises(ObjectStoreUploadError, match="report\\.md"):
        storage.upload(local_path, "kind=bench/dt=2026-10-06/report.md")


def test_existing_keys_strips_the_prefix_back_off(tmp_path: Path) -> None:
    client = _FakeClient(
        existing_keys={
            "prod/kind=bench/dt=2026-10-06/a.md",
            "prod/kind=bench/dt=2026-10-06/b.md",
            "prod/kind=soak/dt=2026-10-02/c.log",
        }
    )
    storage = S3CompatibleStorage(bucket="my-bucket", key_prefix="prod", client=client)

    keys = storage.existing_keys("kind=bench/")

    assert keys == {
        "kind=bench/dt=2026-10-06/a.md",
        "kind=bench/dt=2026-10-06/b.md",
    }


def test_existing_keys_with_no_prefix_configured() -> None:
    client = _FakeClient(existing_keys={"kind=bench/dt=2026-10-06/a.md"})
    storage = S3CompatibleStorage(bucket="my-bucket", key_prefix="", client=client)

    assert storage.existing_keys("kind=bench/") == {"kind=bench/dt=2026-10-06/a.md"}


def test_existing_keys_empty_bucket_returns_empty_set() -> None:
    client = _FakeClient(existing_keys=set())
    storage = S3CompatibleStorage(bucket="my-bucket", key_prefix="", client=client)

    assert storage.existing_keys("kind=bench/") == frozenset()


def test_existing_keys_wraps_a_client_failure() -> None:
    client = _FakeClient(fail_list=True)
    storage = S3CompatibleStorage(bucket="my-bucket", key_prefix="", client=client)

    with pytest.raises(ObjectStoreUploadError, match="my-bucket"):
        storage.existing_keys("kind=bench/")


def test_key_prefix_normalizes_a_double_trailing_slash(tmp_path: Path) -> None:
    """`key_prefix="prod//"` must round-trip through upload/existing_keys exactly
    like `"prod/"` or `"prod"` — `_prefix_root` normalizes once at construction
    rather than leaving `_full_key`/`existing_keys` to disagree on the literal
    prefix string.
    """
    local_path = tmp_path / "a.md"
    local_path.write_text("hello")
    client = _FakeClient()
    storage = S3CompatibleStorage(
        bucket="my-bucket", key_prefix="prod//", client=client
    )

    storage.upload(local_path, "kind=bench/dt=2026-10-06/a.md")

    assert client.put_calls[0][1] == "prod/kind=bench/dt=2026-10-06/a.md"
    assert storage.existing_keys("kind=bench/") == {"kind=bench/dt=2026-10-06/a.md"}
