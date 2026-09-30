"""Renders a `BenchReport` as JSON/markdown and computes issue #53's pass/fail gate.

The gate is computed here, not eyeballed from the raw numbers, so a bench run's
pass/fail is reproducible: accuracy >= 0.90, zero false act/escalate on the
`obviously_idle`-tagged fixture subset, zero parse-fallback rate, and p95 `generate()`
latency <= 20% of `[heart.tick] interval_seconds`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ansina.heart.eval.runner import BenchReport
from ansina.heart.tick.decision import TickDecision

_MIN_ACCURACY = 0.90
_MAX_LATENCY_FRACTION_OF_INTERVAL = 0.20


@dataclass(frozen=True, slots=True)
class GateResult:
    """Per-criterion pass/fail plus the overall verdict — `passed` is `all(checks
    .values())`, computed once here so `report_to_markdown` and a caller (`__main__.py`,
    a future CI-adjacent script) can never read the same report and disagree.
    """

    passed: bool
    checks: dict[str, bool]
    latency_threshold_seconds: float


def gate_result(report: BenchReport, *, interval_seconds: float) -> GateResult:
    latency_threshold = interval_seconds * _MAX_LATENCY_FRACTION_OF_INTERVAL
    checks = {
        "accuracy >= 0.90": report.accuracy >= _MIN_ACCURACY,
        "zero false act/escalate on obviously_idle fixtures": (
            report.false_act_or_escalate_on_obvious_idle == 0
        ),
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


def report_to_json(report: BenchReport, *, gate: GateResult) -> str:
    """Machine-readable form — every field `report_to_markdown` renders, plus the raw
    per-fixture results the markdown table only summarizes.
    """
    payload = {
        "model_repo": report.model_repo,
        "prompt_variant": report.prompt_variant,
        "chat_template": report.chat_template,
        "generated_at": report.generated_at,
        "host": report.host,
        "mlx_lm_version": report.mlx_lm_version,
        "max_output_tokens": report.max_output_tokens,
        "fixture_count": report.fixture_count,
        "metrics": {
            "accuracy": report.accuracy,
            "recall_by_class": {
                d.value: report.recall_by_class[d] for d in TickDecision
            },
            "class_counts": {d.value: report.class_counts[d] for d in TickDecision},
            "recall_by_tag": dict(report.recall_by_tag),
            "tag_counts": dict(report.tag_counts),
            "parse_fallback_rate": report.parse_fallback_rate,
            "false_act_or_escalate_on_obvious_idle": (
                report.false_act_or_escalate_on_obvious_idle
            ),
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
                "raw_output": r.raw_output,
                "latency_seconds": r.latency_seconds,
                "prompt_tokens": r.prompt_tokens,
                "tags": sorted(r.tags),
            }
            for r in report.results
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def report_to_markdown(report: BenchReport, *, gate: GateResult) -> str:
    lines = [
        f"# Heart bench: {report.model_repo} / {report.prompt_variant}",
        "",
        f"- Generated: {report.generated_at}",
        f"- Host: {report.host}",
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
            f"| Parse-fallback rate | {report.parse_fallback_rate:.2%} |",
            f"| False act/escalate on obviously-idle | "
            f"{report.false_act_or_escalate_on_obvious_idle} |",
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
        f"| {d.value} | {report.recall_by_class[d]:.2%} | {report.class_counts[d]} |"
        for d in TickDecision
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
            "## Per-fixture results",
            "",
            "| id | expected | actual | correct | latency (s) | tokens |",
            "|---|---|---|---|---|---|",
        ]
    )
    lines.extend(
        f"| {r.fixture_id} | {r.expected.value} | "
        f"{r.actual.value if r.actual is not None else '(fallback)'} | "
        f"{'✅' if r.correct else '❌'} | {r.latency_seconds:.3f} | {r.prompt_tokens} |"
        for r in report.results
    )
    lines.append("")
    return "\n".join(lines)
