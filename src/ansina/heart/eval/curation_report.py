"""Renders a `CurationReport` as JSON/markdown and computes issue #14's pass/fail
gate.

Gate thresholds, committed before the first real bench run per the issue's own
"define it as part of doing this work" instruction:

1. Curated identifies the run's failure cause in strictly more fixtures than the
   recency-truncated baseline does (`curated_identified_count -
   truncated_identified_count >= 1`) — the issue's own primary metric. At this
   suite's small fixture count (4-6, decided with the project owner ahead of the
   first real run), a percentage-point margin would be too coarse to be meaningful;
   "beats it by at least one fixture" is the simplest honest threshold at this scale.
   A tie (0) or a loss rejects, per the issue's own "a tie rejects — truncation is
   free" rule.
2. Must-keep recall == 100% across every fixture — strict for a first small-N run:
   no fixture may lose a labelled must-keep event from the curator's own selection.
3. Zero parse-fallback rate — mirrors every other suite's own gate clause.
4. p95 curation latency stays under the identical `interval_seconds`-relative ceiling
   `heart.eval.report.gate_result` already computes for the tick suite
   (`MAX_LATENCY_FRACTION_OF_INTERVAL`, reused rather than re-derived, so the two
   suites can never silently disagree about what "too slow" means).

Reported but never gated: `exceeded_budget_count` (how often the curator's own raw
selection didn't fit the budget unassisted), `truncated_recall` per fixture (the
baseline is free to lose a must-keep event — that's exactly the failure mode this
experiment exists to beat), and both raw Brain answers per fixture — the human
sanity-check surface for the primary metric's own keyword-match proxy (see
`heart.eval.curation_runner`'s module docstring for why that proxy is a documented,
coarse stand-in rather than a semantic judge).
"""

from __future__ import annotations

import json

from ansina.heart.eval.curation_runner import CurationReport
from ansina.heart.eval.report import MAX_LATENCY_FRACTION_OF_INTERVAL, GateResult


def curation_gate_result(
    report: CurationReport, *, interval_seconds: float
) -> GateResult:
    latency_threshold = interval_seconds * MAX_LATENCY_FRACTION_OF_INTERVAL
    margin = report.curated_identified_count - report.truncated_identified_count
    checks = {
        "curated beats truncated on identifying the failure cause (margin >= 1)": (
            margin >= 1
        ),
        "must-keep recall == 100%": report.must_keep_recall_rate == 1.0,
        "zero parse-fallback rate": report.parse_fallback_rate == 0.0,
        f"p95 latency <= {latency_threshold:.2f}s "
        f"(20% of {interval_seconds:.1f}s interval)": (
            report.latency_p95_seconds <= latency_threshold
        ),
    }
    return GateResult(
        passed=all(checks.values()),
        checks=checks,
        latency_threshold_seconds=latency_threshold,
    )


def curation_report_to_json(report: CurationReport, *, gate: GateResult) -> str:
    """Machine-readable form — every field `curation_report_to_markdown` renders,
    plus the raw per-fixture results the markdown tables only summarize.
    """
    payload = {
        # See `heart.eval.report.report_to_json`'s matching comment — the
        # discriminator between this family and the other two under the same
        # `kind=bench/` prefix.
        "suite": "curation",
        "model_repo": report.model_repo,
        "prompt_variant": report.prompt_variant,
        "chat_template": report.chat_template,
        "brain_model": report.brain_model,
        "budget_fraction": report.budget_fraction,
        "generated_at": report.generated_at,
        "host": report.host,
        "commit": report.commit,
        "branch": report.branch,
        "mlx_lm_version": report.mlx_lm_version,
        "max_output_tokens": report.max_output_tokens,
        "fixture_count": report.fixture_count,
        "metrics": {
            "must_keep_recall_rate": report.must_keep_recall_rate,
            "curated_identified_count": report.curated_identified_count,
            "truncated_identified_count": report.truncated_identified_count,
            "parse_fallback_rate": report.parse_fallback_rate,
            "exceeded_budget_count": report.exceeded_budget_count,
            "latency_p50_seconds": report.latency_p50_seconds,
            "latency_p95_seconds": report.latency_p95_seconds,
            "prompt_tokens_min": report.prompt_tokens_min,
            "prompt_tokens_median": report.prompt_tokens_median,
            "prompt_tokens_max": report.prompt_tokens_max,
            "peak_rss_bytes": report.peak_rss_bytes,
        },
        "gate": {
            "passed": gate.passed,
            "checks": gate.checks,
            "latency_threshold_seconds": gate.latency_threshold_seconds,
        },
        "results": [
            {
                "id": r.fixture_id,
                "must_keep": sorted(r.must_keep),
                "selected_curated": sorted(r.selected_curated),
                "selected_truncated": sorted(r.selected_truncated),
                "curated_recall": r.curated_recall,
                "truncated_recall": r.truncated_recall,
                "parse_fallback": r.parse_fallback,
                "exceeded_budget": r.exceeded_budget,
                "curated_identified": r.curated_identified,
                "truncated_identified": r.truncated_identified,
                "curated_answer": r.curated_answer,
                "truncated_answer": r.truncated_answer,
                "curation_latency_seconds": r.curation_latency_seconds,
                "prompt_tokens": r.prompt_tokens,
                "tags": sorted(r.tags),
            }
            for r in report.results
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def curation_report_to_markdown(report: CurationReport, *, gate: GateResult) -> str:
    lines = [
        f"# Heart context-curation bench: {report.model_repo} / "
        f"{report.prompt_variant}",
        "",
        f"- Generated: {report.generated_at}",
        f"- Host: {report.host}",
        f"- Commit: {report.commit or 'unknown'}",
        f"- Branch: {report.branch or 'unknown'}",
        f"- mlx-lm: {report.mlx_lm_version or 'unknown'}",
        f"- Chat template applied: {report.chat_template}",
        f"- Brain model: {report.brain_model}",
        f"- Budget fraction: {report.budget_fraction:.0%} of each fixture's own "
        "full-transcript tokens",
        f"- Max output tokens: {report.max_output_tokens}",
        f"- Fixtures: {report.fixture_count}",
        "",
        f"## Gate: {'PASS' if gate.passed else 'FAIL'}",
        "",
        "| Check | Result |",
        "|---|---|",
    ]
    lines.extend(
        f"| {name} | {'✅ pass' if ok else '❌ fail'} |"
        for name, ok in gate.checks.items()
    )
    lines.extend(
        [
            "",
            "## Metrics",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Curated identified failure cause | "
            f"{report.curated_identified_count} / {report.fixture_count} |",
            f"| Truncated (baseline) identified failure cause | "
            f"{report.truncated_identified_count} / {report.fixture_count} |",
            f"| Must-keep recall | {report.must_keep_recall_rate:.2%} |",
            f"| Parse-fallback rate | {report.parse_fallback_rate:.2%} |",
            f"| Curator exceeded its own budget | "
            f"{report.exceeded_budget_count} / {report.fixture_count} |",
            f"| p50 curation latency | {report.latency_p50_seconds:.3f}s |",
            f"| p95 curation latency | {report.latency_p95_seconds:.3f}s |",
            f"| Prompt tokens (min/median/max) | "
            f"{report.prompt_tokens_min} / {report.prompt_tokens_median:.0f} / "
            f"{report.prompt_tokens_max} |",
            f"| Peak RSS | {report.peak_rss_bytes / (1024**3):.2f} GiB |",
            "",
            "## Per-fixture results",
            "",
            "| id | must-keep recall | curated identified | truncated identified | "
            "parse fallback | latency (s) |",
            "|---|---|---|---|---|---|",
        ]
    )
    lines.extend(
        f"| {r.fixture_id} | {'✅' if r.curated_recall else '❌'} | "
        f"{'✅' if r.curated_identified else '❌'} | "
        f"{'✅' if r.truncated_identified else '❌'} | "
        f"{'yes' if r.parse_fallback else 'no'} | "
        f"{r.curation_latency_seconds:.3f} |"
        for r in report.results
    )
    lines.extend(
        [
            "",
            "## Per-fixture answers "
            "(human sanity-check surface for the keyword-match proxy)",
            "",
        ]
    )
    for r in report.results:
        lines.extend(
            [
                f"### {r.fixture_id}",
                "",
                f"**Curated answer:** {r.curated_answer}",
                "",
                f"**Truncated (baseline) answer:** {r.truncated_answer}",
                "",
            ]
        )
    return "\n".join(lines)
