from __future__ import annotations

from pathlib import Path

from ansina.ml.contract import (
    SCHEMA_PATH,
    SCHEMA_VERSION,
    contract_key,
    publish_contract_schema,
)


class _FakeStorage:
    """Mirrors `tests/unit/heart/eval/test_publish.py`'s own `_FakeStorage` double,
    narrowed to just `upload` — `publish_contract_schema` never lists anything.
    """

    def __init__(self) -> None:
        self.uploads: list[tuple[Path, str]] = []

    def upload(self, local_path: Path, key: str) -> None:
        self.uploads.append((local_path, key))

    def existing_keys(self, prefix: str) -> frozenset[str]:
        raise AssertionError(
            "publish_contract_schema must never list existing keys — unlike a "
            "bench/soak item, the contract schema is always re-uploaded"
        )


def test_contract_key_uses_the_default_schema_version() -> None:
    assert contract_key() == f"_contract/corpus-schema-v{SCHEMA_VERSION}.json"


def test_contract_key_accepts_an_explicit_version() -> None:
    assert contract_key(2) == "_contract/corpus-schema-v2.json"


def test_publish_contract_schema_uploads_the_default_path_and_key() -> None:
    storage = _FakeStorage()

    publish_contract_schema(storage)

    assert storage.uploads == [(SCHEMA_PATH, contract_key())]


def test_publish_contract_schema_accepts_overrides(tmp_path: Path) -> None:
    storage = _FakeStorage()
    custom_path = tmp_path / "custom-schema.json"

    publish_contract_schema(storage, schema_path=custom_path, schema_version=2)

    assert storage.uploads == [(custom_path, "_contract/corpus-schema-v2.json")]
