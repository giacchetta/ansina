"""Publishes the corpus contract's machine-readable mirror to the bucket. See issue
#63.

`docs/ml/corpus-contract.md` is the authoritative, human-readable schema document;
`docs/ml/corpus-schema-v1.json` is its hand-maintained, byte-for-byte machine-readable
twin — the two are reviewed together in the same PR whenever either changes, so there
is never a second, independently-generated copy to drift out of sync with it. This
module's only job is uploading that committed file to the bucket's `_contract/`
prefix via issue #59's existing `ReportStorage` port (`ansina.heart.eval.storage`) —
no analysis, no derivation, per issue #63's own "do not write ML/analysis code" scope
line. `heart.eval.storage`'s own module docstring already anticipates this: "(The
`_contract/` schema document itself is issue #63's own concern, published by that
issue's code, not this module's...)".

Deliberately depends only on the `ReportStorage` Protocol, not on
`build_report_storage`/any concrete adapter — the caller
(`heart.eval.publish.main`) already owns constructing the one `storage` instance a
whole run shares; this module never builds its own.
"""

from __future__ import annotations

from pathlib import Path

from ansina.heart.eval.storage import ReportStorage

# Bumped only on a breaking change to any artifact family's shape (a field
# removed/renamed) — see docs/ml/corpus-contract.md's own "stability promise". A
# new, additive family is not a breaking change and does not bump this.
SCHEMA_VERSION = 1

# The committed, hand-maintained machine-readable schema — relative to the
# repository root, the same convention `heart/eval/publish.py`'s own
# `_DEFAULT_BENCH_DIR`/`_DEFAULT_SOAK_DIR` already use.
SCHEMA_PATH = Path("docs/ml/corpus-schema-v1.json")


def contract_key(schema_version: int = SCHEMA_VERSION) -> str:
    """`_contract/corpus-schema-v<schema_version>.json` — prefix-free, the same
    convention `heart.eval.storage.bench_key`/`soak_key` already follow (the
    configured `key_prefix` is applied inside `ReportStorage` itself).
    """
    return f"_contract/corpus-schema-v{schema_version}.json"


def publish_contract_schema(
    storage: ReportStorage,
    *,
    schema_path: Path = SCHEMA_PATH,
    schema_version: int = SCHEMA_VERSION,
) -> None:
    """Uploads `schema_path` to `contract_key(schema_version)`. Unconditional —
    unlike a bench/soak backlog item, this one small file should always reflect
    what's currently committed, never skipped because a same-named key already
    exists. Raises `ObjectStoreUploadError` on failure, same as `ReportStorage
    .upload` itself — the caller decides whether that's fatal.
    """
    storage.upload(schema_path, contract_key(schema_version))
