"""`python -m ansina.heart.eval.publish` — the backlog migration + ongoing catch-up
command behind `make heart-bench-publish`. See issue #59.

Walks the local, gitignored `docs/heart/bench/` and `docs/heart/soak/` directories and
uploads whichever files aren't already in the configured `[telemetry.s3]` bucket,
keyed deterministically (see `heart.eval.storage`'s module docstring for the bucket
layout) so a second run is a verified no-op — the backlog migration *is* just running
this once. Lives under `src/ansina/` rather than `scripts/` so it carries the same
100%-unit-coverage obligation every other `ansina` module does (`scripts/*` tools are
explicitly exempt — see their own module docstrings).

Deliberately a separate command from `heart/eval/__main__.py`'s own upload hook, not a
shared code path: the bench harness uploads the one pair it *just wrote*, suffixing on
a same-day collision so a throwaway tuning run is never lost (see that module's own
docstring); this command mirrors a fixed set of *already-rendered* local files onto
deterministic keys, so a second run touching the same files is a true no-op rather
than minting `-2`/`-3` suffixes for files that were already uploaded once. Skipping
(not suffixing) an already-present key is what makes that distinction possible.

Soak files are walked one level deep, matching the actual on-disk shape
`scripts/heart-soak.sh`/`scripts/heart_soak_report.py` produce — `soak-<run_id>.
{md,json}` directly under `docs/heart/soak/`, plus `<run_id>-raw/` subdirectories
holding `samples.jsonl`/`run.log`/`journal.json` — rather than an unbounded recursive
walk that would have no real shape to validate against.

Any filename this module can't confidently parse a date/run-id out of is **skipped
with a warning**, never guessed — publishing a file under a wrong key would be worse
than not publishing it at all.

Issue #63 adds one more, unconditional step to every real (non-dry-run) run: publish
the committed corpus-contract schema (`ansina.ml.contract.publish_contract_schema`)
to `_contract/corpus-schema-v<N>.json`. Unlike a bench/soak backlog item, that one
small file is never skipped on an existing-key match — it should always reflect
what's currently committed, so it's re-uploaded every run regardless.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ansina.config import load_settings
from ansina.errors import AnsinaError
from ansina.heart.eval.storage import (
    ObjectStoreUnavailableError,
    ObjectStoreUploadError,
    ReportStorage,
    bench_key,
    build_report_storage,
    soak_key,
)
from ansina.logging import configure_logging, get_logger
from ansina.ml.contract import SCHEMA_PATH, contract_key, publish_contract_schema

logger = get_logger(__name__)

_DEFAULT_BENCH_DIR = Path("docs/heart/bench")
_DEFAULT_SOAK_DIR = Path("docs/heart/soak")

_BENCH_EXTENSIONS = (".md", ".json")
_SOAK_TOP_LEVEL_EXTENSIONS = (".md", ".json")
_SOAK_RAW_EXTENSIONS = (".jsonl", ".log", ".json")


@dataclass(frozen=True, slots=True)
class _Item:
    local_path: Path
    key: str


def _looks_like_date(candidate: str) -> bool:
    try:
        date.fromisoformat(candidate)
    except ValueError:
        return False
    return True


def _collect_bench_items(bench_dir: Path) -> list[_Item]:
    """Every `docs/heart/bench/*.{md,json}` file, keyed by `bench_key(filename)` —
    the per-fixture pairing `heart/eval/__main__.py` writes doesn't matter here,
    since each file gets its own independent key.
    """
    if not bench_dir.is_dir():
        return []
    items: list[_Item] = []
    for path in sorted(bench_dir.iterdir()):
        if not path.is_file() or path.suffix not in _BENCH_EXTENSIONS:
            continue
        if not _looks_like_date(path.name[:10]):
            logger.warning(
                "skipping bench file with no leading ISO date",
                extra={"path": str(path)},
            )
            continue
        items.append(_Item(local_path=path, key=bench_key(path.name)))
    return items


def _soak_run_id_from_top_level_name(stem: str) -> str | None:
    """`"soak-2026-10-02"` -> `"2026-10-02"`, `"soak-2026-10-02-2"` ->
    `"2026-10-02-2"` — `None` if `stem` doesn't start with `soak-` followed by a
    leading ISO date.
    """
    prefix = "soak-"
    if not stem.startswith(prefix):
        return None
    run_id = stem[len(prefix) :]
    if not _looks_like_date(run_id[:10]):
        return None
    return run_id


def _soak_run_id_from_raw_dirname(name: str) -> str | None:
    """`"2026-10-02-raw"` -> `"2026-10-02"` — `None` if `name` doesn't end in
    `-raw` or doesn't start with a leading ISO date once that suffix is stripped.
    """
    suffix = "-raw"
    if not name.endswith(suffix):
        return None
    run_id = name[: -len(suffix)]
    if not _looks_like_date(run_id[:10]):
        return None
    return run_id


def _collect_soak_items(soak_dir: Path) -> list[_Item]:
    """Top-level `soak-<run_id>.{md,json}` files, plus every file one level inside a
    `<run_id>-raw/` subdirectory — see module docstring for why this doesn't walk
    deeper than that.
    """
    if not soak_dir.is_dir():
        return []
    items: list[_Item] = []
    for path in sorted(soak_dir.iterdir()):
        if path.is_file() and path.suffix in _SOAK_TOP_LEVEL_EXTENSIONS:
            run_id = _soak_run_id_from_top_level_name(path.stem)
            if run_id is None:
                logger.warning(
                    "skipping soak file with no recognizable run id",
                    extra={"path": str(path)},
                )
                continue
            items.append(_Item(local_path=path, key=soak_key(run_id, path.name)))
        elif path.is_dir():
            run_id = _soak_run_id_from_raw_dirname(path.name)
            if run_id is None:
                logger.warning(
                    "skipping soak subdirectory with no recognizable run id",
                    extra={"path": str(path)},
                )
                continue
            for child in sorted(path.iterdir()):
                if child.is_file() and child.suffix in _SOAK_RAW_EXTENSIONS:
                    items.append(
                        _Item(local_path=child, key=soak_key(run_id, child.name))
                    )
    return items


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ansina.heart.eval.publish",
        description=(
            "Upload every local Heart bench/soak report not already in the "
            "[telemetry.s3] bucket. Running this once is the backlog migration; "
            "running it again is a verified no-op."
        ),
    )
    parser.add_argument(
        "--bench-dir",
        type=Path,
        default=_DEFAULT_BENCH_DIR,
        help=f"Directory to walk for bench reports (default: {_DEFAULT_BENCH_DIR}).",
    )
    parser.add_argument(
        "--soak-dir",
        type=Path,
        default=_DEFAULT_SOAK_DIR,
        help=f"Directory to walk for soak reports (default: {_DEFAULT_SOAK_DIR}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Resolve and print every file's bucket key without constructing an "
            "object-store client or uploading anything — works even with "
            "[telemetry.s3] disabled, since nothing is actually sent."
        ),
    )
    return parser


def _publish_kind(
    storage: ReportStorage, kind_prefix: str, items: list[_Item]
) -> tuple[int, int, int]:
    """Returns `(uploaded, skipped, failed)`. A listing failure is fatal for this
    kind alone (every item counts as failed) — uploading without knowing what's
    already there would defeat the "verified no-op" guarantee.
    """
    try:
        existing = storage.existing_keys(kind_prefix)
    except ObjectStoreUploadError as exc:
        logger.warning("failed to list existing keys", extra={"error": str(exc)})
        return 0, 0, len(items)

    uploaded = skipped = failed = 0
    for item in items:
        if item.key in existing:
            skipped += 1
            continue
        try:
            storage.upload(item.local_path, item.key)
        except ObjectStoreUploadError as exc:
            logger.warning(
                "failed to upload report",
                extra={"path": str(item.local_path), "error": str(exc)},
            )
            failed += 1
            continue
        print(f"uploaded {item.local_path} -> {item.key}")
        uploaded += 1
    return uploaded, skipped, failed


def _publish_contract(storage: ReportStorage) -> bool:
    """Issue #63: always re-uploads the committed `docs/ml/corpus-schema-v1.json` —
    unlike a bench/soak backlog item, this one small file should always reflect
    what's currently committed, never skipped because a same-named key already
    exists. Returns `False` (logged, never raised) on any upload failure, the same
    best-effort-but-counted-as-failed shape `_publish_kind` already uses for a
    single item.
    """
    try:
        publish_contract_schema(storage)
    except ObjectStoreUploadError as exc:
        logger.warning("failed to publish contract schema", extra={"error": str(exc)})
        return False
    print(f"published contract schema -> {contract_key()}")
    return True


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        settings = load_settings()
    except AnsinaError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    configure_logging(settings)

    bench_items = _collect_bench_items(args.bench_dir)
    soak_items = _collect_soak_items(args.soak_dir)

    if args.dry_run:
        for item in (*bench_items, *soak_items):
            print(f"DRY RUN: would upload {item.local_path} -> {item.key}")
        print(
            f"dry run: {len(bench_items) + len(soak_items)} file(s) resolved, "
            "0 uploaded"
        )
        print(f"DRY RUN: would publish {SCHEMA_PATH} -> {contract_key()}")
        return 0

    try:
        storage = build_report_storage(settings.telemetry.s3)
    except ObjectStoreUnavailableError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if storage is None:
        print(
            "telemetry.s3.enabled is false — nothing to publish (set "
            "ANSINA_TELEMETRY__S3__ENABLED=true and configure the bucket/"
            "credentials first)",
            file=sys.stderr,
        )
        return 2

    bench_uploaded, bench_skipped, bench_failed = _publish_kind(
        storage, "kind=bench/", bench_items
    )
    soak_uploaded, soak_skipped, soak_failed = _publish_kind(
        storage, "kind=soak/", soak_items
    )

    uploaded = bench_uploaded + soak_uploaded
    skipped = bench_skipped + soak_skipped
    failed = bench_failed + soak_failed
    print(f"uploaded {uploaded}, skipped {skipped}, {failed} failed")

    contract_published = _publish_contract(storage)

    return 1 if (failed or not contract_published) else 0


if __name__ == "__main__":
    raise SystemExit(main())
