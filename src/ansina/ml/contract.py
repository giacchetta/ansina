"""Publishes the corpus contract's machine-readable mirror to the bucket. See issue
#63, bumped to `schema_version: 2` by issue #67, to `schema_version: 3` by issue
#68, to `schema_version: 4` by issue #76, and to `schema_version: 5` by issue #79.

`docs/ml/corpus-contract.md` is the authoritative, human-readable schema document;
`docs/ml/corpus-schema-v<SCHEMA_VERSION>.json` is its hand-maintained, byte-for-byte
machine-readable twin — the two are reviewed together in the same PR whenever either
changes, so there is never a second, independently-generated copy to drift out of
sync with it. This module's only job is uploading that committed file to the
bucket's `_contract/` prefix via issue #59's existing `ReportStorage` port
(`ansina.heart.eval.storage`) — no analysis, no derivation, per issue #63's own "do
not write ML/analysis code" scope line. `heart.eval.storage`'s own module docstring
already anticipates this: "(The `_contract/` schema document itself is issue #63's
own concern, published by that issue's code, not this module's...)".

Deliberately depends only on the `ReportStorage` Protocol, not on
`build_report_storage`/any concrete adapter — the caller
(`heart.eval.publish.main`) already owns constructing the one `storage` instance a
whole run shares; this module never builds its own.

Issue #67 bumped `SCHEMA_VERSION`/`SCHEMA_PATH` from 1 to 2 — a breaking change to
the `kind=telemetry/`/`kind=runlog/` key layout (a new `host=<id>/` segment ahead
of `dt=<date>/`, closing v1's own documented "no host attribution" gap), per
`docs/ml/corpus-contract.md`'s own stability promise. Issue #68 bumps it again, to
3 — adding `idle_total`/`act_total`/`escalate_total` to `telemetry_sample` is, by
that same stability promise, a minor/additive change on its own (a new
optional/nullable field on an existing family) and would not ordinarily bump this;
it is bumped here regardless, per that issue's own explicit acceptance criterion,
for discoverability of the fix to the documented 2:1 `last_decision`-undercounting
gap — see `docs/ml/corpus-contract.md`'s own "Exact decision counts" section.
Issue #76 bumps it again, to 4 — adding the new `kind=opencode/` family
(`opencode_event`, produced by `ansina.opencode.telemetry.OpencodeTelemetryWriter`)
is, by the stability promise's own text, a **minor**, non-breaking addition (a new
artifact family) that would not on its own require a bump; it is bumped here
regardless, the same discoverability reasoning issue #68 already established for
its own additive change — see `docs/ml/corpus-contract.md`'s own changelog entry.
Issue #79 bumps it again, to 5 — populating `opencode_event`'s own already-
reserved `"intervention"` event (`action`/`outcome`/`detail` fields, the Heart's
bounded interventions on the OpenCode executor) is, by the same stability
promise, also non-breaking on its own (the event name was already documented as
reserved in `schema_version: 4`); bumped anyway, the identical discoverability
reasoning #68/#76 already established.
`docs/ml/corpus-schema-v1.json`/`corpus-schema-v2.json`/`corpus-schema-v3.json`/
`corpus-schema-v4.json` stay committed and are never re-uploaded by this module's
own default — a prior version, once published, is never deleted or overwritten.
"""

from __future__ import annotations

from pathlib import Path

from ansina.heart.eval.storage import ReportStorage

# Bumped on a breaking change to any artifact family's shape (a field
# removed/renamed), or — as of issue #68 — on an explicit acceptance criterion
# calling for it even on an additive change; see docs/ml/corpus-contract.md's own
# "stability promise" and this module's own docstring above.
SCHEMA_VERSION = 5

# The committed, hand-maintained machine-readable schema — relative to the
# repository root, the same convention `heart/eval/publish.py`'s own
# `_DEFAULT_BENCH_DIR`/`_DEFAULT_SOAK_DIR` already use.
SCHEMA_PATH = Path("docs/ml/corpus-schema-v5.json")


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
