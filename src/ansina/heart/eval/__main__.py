"""`python -m ansina.heart.eval` — the real-model bench entry point. See issue #53
(tick suite) / #13 (triage suite).

Never invoked in CI: no MLX adapter is viable on either CI leg (see
`ansina.heart.selection`). Run via `make heart-bench` on the Mac Mini M4, which wraps
`uv run --extra mlx python -m ansina.heart.eval`. Its logic is covered by
`tests/unit/heart/eval/test_main.py` against a fake `HeartRuntime`, not by running the
real thing in CI — the same "structured so the entry point stays thin, coverage comes
from unit-testing what it calls" shape `ansina/__main__.py` already follows.

Reads `[heart]`/`[heart.tick]` from the normal `load_settings()` layering;
`--model-repo` and `--max-output-tokens` override those two fields for this run only,
via `Settings.model_copy`, without touching `ansina.toml` or the environment.

`--suite {tick,triage}` (default `tick`) picks which bench runs — issue #13's own
request-triage experiment reuses this entry point, the runtime lifecycle, provenance
resolution, and the upload hook rather than duplicating any of them; only the fixture
loader, the prompt-variant map, and the report renderer differ per suite, each kept in
`_run_tick_suite`/`_run_triage_suite` below.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ansina.config import Settings, load_settings
from ansina.errors import AnsinaError
from ansina.heart.eval.fixtures import (
    TickFixture,
    TriageFixture,
    load_fixtures,
    load_triage_fixtures,
)
from ansina.heart.eval.provenance import Provenance, resolve_provenance
from ansina.heart.eval.report import gate_result, report_to_json, report_to_markdown
from ansina.heart.eval.runner import run_bench
from ansina.heart.eval.storage import (
    bench_key,
    build_report_storage,
    next_available_stem,
)
from ansina.heart.eval.triage_report import (
    triage_gate_result,
    triage_report_to_json,
    triage_report_to_markdown,
)
from ansina.heart.eval.triage_runner import run_triage_bench
from ansina.heart.runtime import HeartRuntime
from ansina.heart.selection import build_heart_runtime
from ansina.heart.tick.prompts import DEFAULT_PROMPT_VARIANT, PROMPT_VARIANTS
from ansina.heart.triage import (
    DEFAULT_TRIAGE_VARIANT,
    TRIAGE_PROMPT_VARIANTS,
    HeartRequestTriage,
)
from ansina.logging import configure_logging, get_logger

_DEFAULT_OUT_DIR = Path("docs/heart/bench")

# The union of both suites' variant names, purely for `argparse`'s own `--help`
# rendering — the actual choice is validated against the *selected* suite's map in
# `main()`, since the two suites don't share a prompt-variant namespace (issue #13's
# own "strict" is an unrelated template from the tick suite's "strict").
_ALL_PROMPT_VARIANTS = sorted(set(PROMPT_VARIANTS) | set(TRIAGE_PROMPT_VARIANTS))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ansina.heart.eval",
        description="Bench a HeartRuntime against a labelled fixture set.",
    )
    parser.add_argument(
        "--suite",
        choices=("tick", "triage"),
        default="tick",
        help=(
            "Which bench to run: 'tick' (issue #53, the shipped autonomic-loop "
            "decision) or 'triage' (issue #13, the gated request-triage "
            "experiment — wired into nothing, bench only). Default: tick."
        ),
    )
    parser.add_argument(
        "--model-repo",
        default=None,
        help="Override [heart] model_repo for this run only.",
    )
    parser.add_argument(
        "--prompt-variant",
        choices=_ALL_PROMPT_VARIANTS,
        default=None,
        help=(
            "Named prompt template. Validated against the selected --suite's own "
            "variant map (ansina.heart.tick.prompts.PROMPT_VARIANTS for 'tick', "
            "ansina.heart.triage.TRIAGE_PROMPT_VARIANTS for 'triage'); defaults to "
            "that suite's own default variant when omitted."
        ),
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=None,
        help="Path to a JSONL fixture set, overriding the bundled default.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=_DEFAULT_OUT_DIR,
        help=f"Directory to write the report pair into (default: {_DEFAULT_OUT_DIR}).",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=None,
        help="Override [heart] max_output_tokens for this run only.",
    )
    parser.add_argument(
        "--no-chat-template",
        action="store_true",
        help=(
            "Disable [heart] apply_chat_template for this run only — the pre-#53 "
            "raw-prompt behavior, kept to reproduce the earliest bench report."
        ),
    )
    return parser


def _upload_report_pair(settings: Settings, out_dir: Path, stem: str) -> None:
    """Issue #59's upload hook. Best-effort: the pair at `stem` is always written
    locally first (the two `write_text` calls just above this function's one call
    site, in `main()`) and kept either way — this never deletes or changes what's
    on disk. Any failure at all — `[telemetry.s3] enabled = false` (the common
    case, handled as a quiet no-op, not a warning), a missing bucket/bad
    credentials, an unreachable endpoint, or a real upload error — is logged and
    swallowed, never raised: this is a bench/gate tool, and a flaky upload must
    never flip `main()`'s own exit code, the same "fail loudly only for what the
    caller actually asked for" boundary `heart.models._download_from_hub`'s own
    broad `except Exception` draws around a third-party failure.

    Lists the bucket first and suffixes (`next_available_stem`) on a same-day,
    same-model, same-variant collision — the exact local discipline
    `scripts/remote-heart.sh` already applies to `docs/heart/bench/`, extended to
    the bucket, so a second run on the same day never silently overwrites the
    first. Only the *uploaded key* is suffixed; the local files stay named `stem`.
    Suite-agnostic: both the tick and triage suites key under `kind=bench/`.
    """
    try:
        storage = build_report_storage(settings.telemetry.s3)
        if storage is None:
            return
        existing = storage.existing_keys("kind=bench/")
        final_stem = next_available_stem(stem, existing)
        for suffix in ("md", "json"):
            storage.upload(
                out_dir / f"{stem}.{suffix}", bench_key(f"{final_stem}.{suffix}")
            )
    except Exception as exc:  # see docstring: best-effort by design, never raises
        get_logger(__name__).warning(
            "heart bench: report upload failed",
            extra={"stem": stem, "error": str(exc)},
        )


def _run_tick_suite(
    runtime: HeartRuntime,
    settings: Settings,
    *,
    fixtures: tuple[TickFixture, ...],
    prompt_variant: str,
    provenance: Provenance,
) -> tuple[str, str, str, bool]:
    """Run the tick-decision bench (issue #53) and return `(stem, markdown, json,
    gate_passed)` — `runtime` must already be loaded; this function never calls
    `load()`/`unload()`, the same division of responsibility `run_bench` itself
    documents.
    """
    template = PROMPT_VARIANTS[prompt_variant]
    budget_tokens = max(0, runtime.context_tokens - settings.heart.max_output_tokens)
    report = run_bench(
        runtime,
        fixtures,
        budget_tokens=budget_tokens,
        max_output_tokens=settings.heart.max_output_tokens,
        template=template,
        prompt_variant=prompt_variant,
        model_repo=settings.heart.model_repo,
        chat_template=settings.heart.apply_chat_template,
        commit=provenance.commit,
        branch=provenance.branch,
    )
    gate = gate_result(report, interval_seconds=settings.heart.tick.interval_seconds)

    date = report.generated_at[:10]
    model_slug = settings.heart.model_repo.rsplit("/", 1)[-1]
    template_suffix = "" if settings.heart.apply_chat_template else "-notemplate"
    stem = f"{date}-{model_slug}-{prompt_variant}{template_suffix}"
    return (
        stem,
        report_to_markdown(report, gate=gate),
        report_to_json(report, gate=gate),
        gate.passed,
    )


def _run_triage_suite(
    runtime: HeartRuntime,
    settings: Settings,
    *,
    fixtures: tuple[TriageFixture, ...],
    prompt_variant: str,
    provenance: Provenance,
) -> tuple[str, str, str, bool]:
    """Run the request-triage bench (issue #13, the gated experiment — wired into
    nothing beyond this bench) and return `(stem, markdown, json, gate_passed)`.
    `runtime` must already be loaded, same discipline as `_run_tick_suite`.
    """
    template = TRIAGE_PROMPT_VARIANTS[prompt_variant]
    triage = HeartRequestTriage(
        runtime, template=template, max_output_tokens=settings.heart.max_output_tokens
    )
    report = run_triage_bench(
        triage,
        fixtures,
        model_repo=settings.heart.model_repo,
        prompt_variant=prompt_variant,
        max_output_tokens=settings.heart.max_output_tokens,
        chat_template=settings.heart.apply_chat_template,
        commit=provenance.commit,
        branch=provenance.branch,
    )
    gate = triage_gate_result(report)

    date = report.generated_at[:10]
    model_slug = settings.heart.model_repo.rsplit("/", 1)[-1]
    template_suffix = "" if settings.heart.apply_chat_template else "-notemplate"
    stem = f"{date}-{model_slug}-triage-{prompt_variant}{template_suffix}"
    return (
        stem,
        triage_report_to_markdown(report, gate=gate),
        triage_report_to_json(report, gate=gate),
        gate.passed,
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        settings = load_settings()
    except AnsinaError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    configure_logging(settings)
    logger = get_logger(__name__)

    heart_overrides: dict[str, object] = {}
    if args.model_repo is not None:
        heart_overrides["model_repo"] = args.model_repo
    if args.max_output_tokens is not None:
        heart_overrides["max_output_tokens"] = args.max_output_tokens
    if args.no_chat_template:
        heart_overrides["apply_chat_template"] = False
    if heart_overrides:
        settings = settings.model_copy(
            update={"heart": settings.heart.model_copy(update=heart_overrides)}
        )

    variant_map = PROMPT_VARIANTS if args.suite == "tick" else TRIAGE_PROMPT_VARIANTS
    default_variant = (
        DEFAULT_PROMPT_VARIANT if args.suite == "tick" else DEFAULT_TRIAGE_VARIANT
    )
    prompt_variant = args.prompt_variant or default_variant
    if prompt_variant not in variant_map:
        print(
            f"--prompt-variant {prompt_variant!r} is not a variant of the "
            f"{args.suite!r} suite (choices: {sorted(variant_map)})",
            file=sys.stderr,
        )
        return 1

    try:
        if args.suite == "tick":
            tick_fixtures = load_fixtures(args.fixtures)
        else:
            triage_fixtures = load_triage_fixtures(args.fixtures)
    except AnsinaError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        runtime = build_heart_runtime(settings)
    except AnsinaError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    provenance = resolve_provenance()

    logger.info(
        "heart bench: loading model", extra={"model_repo": settings.heart.model_repo}
    )
    runtime.load()
    try:
        if args.suite == "tick":
            stem, markdown, json_text, passed = _run_tick_suite(
                runtime,
                settings,
                fixtures=tick_fixtures,
                prompt_variant=prompt_variant,
                provenance=provenance,
            )
        else:
            stem, markdown, json_text, passed = _run_triage_suite(
                runtime,
                settings,
                fixtures=triage_fixtures,
                prompt_variant=prompt_variant,
                provenance=provenance,
            )
    finally:
        runtime.unload()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / f"{stem}.md").write_text(markdown)
    (args.out_dir / f"{stem}.json").write_text(json_text)

    _upload_report_pair(settings, args.out_dir, stem)

    logger.info(
        "heart bench: complete",
        extra={
            "suite": args.suite,
            "gate_passed": passed,
            "out_dir": str(args.out_dir),
        },
    )
    verdict = "PASS" if passed else "FAIL"
    print(f"{verdict}: wrote {stem}.md / {stem}.json to {args.out_dir}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
