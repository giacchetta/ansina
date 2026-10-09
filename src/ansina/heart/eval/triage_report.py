"""Renders a `TriageReport` as JSON/markdown and computes issue #13's pass/fail gate.

Gate thresholds, committed before the first real bench run per the issue's own "define
it as part of doing this work" instruction:

1. zero `complex`->`trivial` misroutes — the issue's own reject condition: answering
   directly what needed the Brain is a correctness bug, the same zero-tolerance
   severity #53's bench gate gives a false-`idle` on an `obviously_idle` tick fixture.
2. zero `tool-only`->`trivial` misroutes — **added** beyond the issue's literal text:
   under today's 2-way routing (no `ToolRegistry` exists yet, so `heart.triage.
   routes_to_brain` sends `tool-only` to the Brain exactly like `complex`), calling a
   `tool-only` request `trivial` is the identical under-route bug as #1.
3. `trivial` recall >= 0.75 — the efficiency ceiling the issue asks to be stated: at
   most 25% of genuinely trivial requests may be needlessly escalated.
4. zero parse-fallback rate — mirrors `heart.eval.report`'s own tick gate clause.
5. p95 triage latency <= 1.0s — **added**: triage latency is pure added latency on
   the (not-yet-existing) request path, before any real work starts; a flat absolute
   ceiling, unlike the tick gate's interval-relative one, since there is no
   `[heart.tick] interval_seconds`-shaped cadence a single triage call answers into.

Reported but never gated: `brain_calls_saved_fraction` (the primary cost-saved figure
this whole experiment exists to measure, compared against the always-escalate
baseline, which saves exactly 0), the full confusion matrix, `recall_by_tag`, and
per-class recall beyond clause 3's `trivial` row.
"""

from __future__ import annotations

import json

from ansina.heart.eval.report import GateResult
from ansina.heart.eval.triage_runner import TriageReport
from ansina.heart.triage import TriageClass

MIN_TRIVIAL_RECALL = 0.75
MAX_LATENCY_P95_SECONDS = 1.0


def triage_gate_result(report: TriageReport) -> GateResult:
    checks = {
        "zero complex->trivial misroutes": report.misroute_complex_to_trivial == 0,
        "zero tool-only->trivial misroutes": (
            report.misroute_tool_only_to_trivial == 0
        ),
        f"trivial recall >= {MIN_TRIVIAL_RECALL:.0%}": (
            report.recall_by_class[TriageClass.TRIVIAL] >= MIN_TRIVIAL_RECALL
        ),
        "zero parse-fallback rate": report.parse_fallback_rate == 0.0,
        f"p95 latency <= {MAX_LATENCY_P95_SECONDS:.2f}s": (
            report.latency_p95_seconds <= MAX_LATENCY_P95_SECONDS
        ),
    }
    return GateResult(
        passed=all(checks.values()),
        checks=checks,
        latency_threshold_seconds=MAX_LATENCY_P95_SECONDS,
    )


def triage_report_to_json(report: TriageReport, *, gate: GateResult) -> str:
    """Machine-readable form — every field `triage_report_to_markdown` renders, plus
    the raw per-fixture results the markdown table only summarizes.
    """
    payload = {
        # See `heart.eval.report.report_to_json`'s matching comment — the
        # discriminator between the two families under the same `kind=bench/` prefix.
        "suite": "triage",
        "model_repo": report.model_repo,
        "prompt_variant": report.prompt_variant,
        "chat_template": report.chat_template,
        "generated_at": report.generated_at,
        "host": report.host,
        "commit": report.commit,
        "branch": report.branch,
        "mlx_lm_version": report.mlx_lm_version,
        "max_output_tokens": report.max_output_tokens,
        "fixture_count": report.fixture_count,
        "metrics": {
            "accuracy": report.accuracy,
            "recall_by_class": {
                c.value: report.recall_by_class[c] for c in TriageClass
            },
            "class_counts": {c.value: report.class_counts[c] for c in TriageClass},
            "recall_by_tag": dict(report.recall_by_tag),
            "tag_counts": dict(report.tag_counts),
            "parse_fallback_rate": report.parse_fallback_rate,
            "misroute_complex_to_trivial": report.misroute_complex_to_trivial,
            "misroute_tool_only_to_trivial": report.misroute_tool_only_to_trivial,
            "over_route_count": report.over_route_count,
            "over_route_rate": report.over_route_rate,
            "brain_calls_saved_fraction": report.brain_calls_saved_fraction,
            "confusion": {c.value: dict(row) for c, row in report.confusion.items()},
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
                "expected": r.expected.value,
                "actual": r.actual.value if r.actual is not None else None,
                "correct": r.correct,
                "parse_fallback": r.parse_fallback,
                "routed_to_brain": r.routed_to_brain,
                "under_route": r.under_route,
                "over_route": r.over_route,
                "raw_output": r.raw_output,
                "latency_seconds": r.latency_seconds,
                "prompt_tokens": r.prompt_tokens,
                "tags": sorted(r.tags),
            }
            for r in report.results
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def triage_report_to_markdown(report: TriageReport, *, gate: GateResult) -> str:
    lines = [
        f"# Heart triage bench: {report.model_repo} / {report.prompt_variant}",
        "",
        f"- Generated: {report.generated_at}",
        f"- Host: {report.host}",
        f"- Commit: {report.commit or 'unknown'}",
        f"- Branch: {report.branch or 'unknown'}",
        f"- mlx-lm: {report.mlx_lm_version or 'unknown'}",
        f"- Chat template applied: {report.chat_template}",
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
            f"| Overall accuracy | {report.accuracy:.2%} |",
            f"| Brain calls saved vs. always-escalate | "
            f"{report.brain_calls_saved_fraction:.2%} |",
            f"| Over-route rate | {report.over_route_rate:.2%} "
            f"({report.over_route_count}) |",
            f"| complex→trivial misroutes | {report.misroute_complex_to_trivial} |",
            f"| tool-only→trivial misroutes | {report.misroute_tool_only_to_trivial} |",
            f"| Parse-fallback rate | {report.parse_fallback_rate:.2%} |",
            f"| p50 latency | {report.latency_p50_seconds:.3f}s |",
            f"| p95 latency | {report.latency_p95_seconds:.3f}s |",
            f"| Prompt tokens (min/median/max) | "
            f"{report.prompt_tokens_min} / {report.prompt_tokens_median:.0f} / "
            f"{report.prompt_tokens_max} |",
            f"| Peak RSS | {report.peak_rss_bytes / (1024**3):.2f} GiB |",
            "",
            "## Per-class recall",
            "",
            "| Class | Recall | Count |",
            "|---|---|---|",
        ]
    )
    lines.extend(
        f"| {c.value} | {report.recall_by_class[c]:.2%} | {report.class_counts[c]} |"
        for c in TriageClass
    )
    lines.extend(
        [
            "",
            "## Recall by tag",
            "",
            "| Tag | Recall | Count |",
            "|---|---|---|",
        ]
    )
    lines.extend(
        f"| {tag} | {report.recall_by_tag[tag]:.2%} | {report.tag_counts[tag]} |"
        for tag in sorted(report.recall_by_tag)
    )
    lines.extend(
        [
            "",
            "## Confusion matrix (rows: expected, columns: actual)",
            "",
            "| expected \\ actual | "
            + " | ".join(c.value for c in TriageClass)
            + " | unparsed |",
            "|---|" + "---|" * (len(TriageClass) + 1),
        ]
    )
    lines.extend(
        f"| {expected.value} | "
        + " | ".join(str(report.confusion[expected][c.value]) for c in TriageClass)
        + f" | {report.confusion[expected]['unparsed']} |"
        for expected in TriageClass
    )
    lines.extend(
        [
            "",
            "## Per-fixture results",
            "",
            "| id | expected | actual | correct | routed to brain | "
            "latency (s) | tokens |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    lines.extend(
        f"| {r.fixture_id} | {r.expected.value} | "
        f"{r.actual.value if r.actual is not None else '(fallback)'} | "
        f"{'✅' if r.correct else '❌'} | {'yes' if r.routed_to_brain else 'no'} | "
        f"{r.latency_seconds:.3f} | {r.prompt_tokens} |"
        for r in report.results
    )
    lines.append("")
    return "\n".join(lines)
