"""`python -m ansina.heart.eval` — the real-model bench entry point. See issue #53.

Never invoked in CI: no MLX adapter is viable on either CI leg (see
`ansina.heart.selection`). Run via `make heart-bench` on the Mac Mini M4, which wraps
`uv run --extra mlx python -m ansina.heart.eval`. Its logic is covered by
`tests/unit/heart/eval/test_main.py` against a fake `HeartRuntime`, not by running the
real thing in CI — the same "structured so the entry point stays thin, coverage comes
from unit-testing what it calls" shape `ansina/__main__.py` already follows.

Reads `[heart]`/`[heart.tick]` from the normal `load_settings()` layering;
`--model-repo` and `--max-output-tokens` override those two fields for this run only,
via `Settings.model_copy`, without touching `ansina.toml` or the environment.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ansina.config import load_settings
from ansina.errors import AnsinaError
from ansina.heart.eval.fixtures import load_fixtures
from ansina.heart.eval.provenance import resolve_provenance
from ansina.heart.eval.report import gate_result, report_to_json, report_to_markdown
from ansina.heart.eval.runner import run_bench
from ansina.heart.selection import build_heart_runtime
from ansina.heart.tick.prompts import DEFAULT_PROMPT_VARIANT, PROMPT_VARIANTS
from ansina.logging import configure_logging, get_logger

_DEFAULT_OUT_DIR = Path("docs/heart/bench")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ansina.heart.eval",
        description="Bench a HeartRuntime against the labelled tick fixture set.",
    )
    parser.add_argument(
        "--model-repo",
        default=None,
        help="Override [heart] model_repo for this run only.",
    )
    parser.add_argument(
        "--prompt-variant",
        choices=sorted(PROMPT_VARIANTS),
        default=DEFAULT_PROMPT_VARIANT,
        help="Named prompt template (ansina.heart.tick.prompts.PROMPT_VARIANTS).",
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

    try:
        fixtures = load_fixtures(args.fixtures)
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
        template = PROMPT_VARIANTS[args.prompt_variant]
        budget_tokens = max(
            0, runtime.context_tokens - settings.heart.max_output_tokens
        )
        report = run_bench(
            runtime,
            fixtures,
            budget_tokens=budget_tokens,
            max_output_tokens=settings.heart.max_output_tokens,
            template=template,
            prompt_variant=args.prompt_variant,
            model_repo=settings.heart.model_repo,
            chat_template=settings.heart.apply_chat_template,
            commit=provenance.commit,
            branch=provenance.branch,
        )
    finally:
        runtime.unload()

    gate = gate_result(report, interval_seconds=settings.heart.tick.interval_seconds)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    date = report.generated_at[:10]
    model_slug = settings.heart.model_repo.rsplit("/", 1)[-1]
    template_suffix = "" if settings.heart.apply_chat_template else "-notemplate"
    stem = f"{date}-{model_slug}-{args.prompt_variant}{template_suffix}"
    (args.out_dir / f"{stem}.md").write_text(report_to_markdown(report, gate=gate))
    (args.out_dir / f"{stem}.json").write_text(report_to_json(report, gate=gate))

    logger.info(
        "heart bench: complete",
        extra={
            "gate_passed": gate.passed,
            "accuracy": report.accuracy,
            "out_dir": str(args.out_dir),
        },
    )
    verdict = "PASS" if gate.passed else "FAIL"
    print(f"{verdict}: wrote {stem}.md / {stem}.json to {args.out_dir}")
    return 0 if gate.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
