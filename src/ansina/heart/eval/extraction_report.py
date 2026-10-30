"""Renders an `ExtractionReport` as JSON/markdown and computes issue #15's three
pass/fail gates — **one per job, gated separately**, per the issue's own explicit
instruction: "Jobs 2 and 3 are measured and reported but gated separately — a pass
on titling does not license swapping the intervention policy." Only
`intervention_gate_result` governs the adoption decision; `title_gate_result`/
`failure_gate_result` are reported, informational, and never fold into it.

Gate thresholds, committed before the first real bench run per the issue's own
"define it as part of doing this work" instruction:

**Job 1 (intervention selection) — the one that gates adoption:**

1. Zero parse-fallback rate — the strict parser in `heart.extraction` already
   refuses a hallucinated `run_id` or an out-of-vocabulary `action`, so this one
   clause covers both of the issue's own zero-tolerance ceilings.
2. Zero `"less_conservative"` disagreements — the issue's own named correctness-bug
   ceiling (picking a less-conservative action than the deterministic policy would,
   e.g. `retry` where it says `pause_intake`).
3. Zero `"wrong_run"` disagreements — the right action, the wrong target: not named
   explicitly in the issue's own text, but the identical severity class (acting on
   the wrong run is not a "slightly less efficient" outcome).
4. `"more_conservative"` disagreement rate <= `MAX_MORE_CONSERVATIVE_RATE` — the
   issue's own named efficiency-loss ceiling, tolerated up to a stated bound rather
   than zero.
5. p95 latency <= `MAX_LATENCY_P95_SECONDS` — a flat absolute ceiling, the same
   `heart.eval.triage_report` reasoning: pure added latency with no
   `[heart.tick] interval_seconds`-shaped cadence for a single extraction call to
   answer into.

**Job 2 (run titling) — reported, never governs adoption:**

6. Schema-valid rate (single-line, non-empty, under the length bound) >=
   `MIN_TITLE_SCHEMA_VALID_RATE`.
7. p95 latency <= `MAX_LATENCY_P95_SECONDS`.

Title *quality* against each fixture's own `expected_title` is never gated — judging
that is exactly the kind of thing this codebase refuses to fake with an unverified
semantic heuristic (see `heart.eval.extraction_runner`'s own module docstring); a
human reads `actual_title` beside `expected_title` instead.

**Job 3 (failure classification) — reported, never governs adoption:**

8. Zero parse-fallback rate.
9. Cause accuracy >= `MIN_FAILURE_CAUSE_ACCURACY`.
10. Evidence-sequence accuracy >= `MIN_FAILURE_EVIDENCE_ACCURACY`.
11. p95 latency <= `MAX_LATENCY_P95_SECONDS`.

Every rate above is computed via `heart.eval.extraction_runner._rate`'s own
0-when-empty convention, so a bench run against a fixture file missing one job
entirely reports that job's gate as a vacuous pass rather than dividing by zero or
raising.
"""

from __future__ import annotations

import json

from ansina.heart.eval.extraction_runner import (
    ExtractionReport,
    InterventionFixtureResult,
)
from ansina.heart.eval.report import GateResult

MAX_MORE_CONSERVATIVE_RATE = 0.20
MAX_LATENCY_P95_SECONDS = 1.0
MIN_TITLE_SCHEMA_VALID_RATE = 0.90
MIN_FAILURE_CAUSE_ACCURACY = 0.75
MIN_FAILURE_EVIDENCE_ACCURACY = 0.75


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def intervention_gate_result(report: ExtractionReport) -> GateResult:
    """The gate that actually licenses a policy swap — see module docstring."""
    more_conservative_rate = _rate(
        report.intervention_more_conservative_count, len(report.intervention_results)
    )
    checks = {
        "zero parse-fallback rate": report.intervention_parse_fallback_rate == 0.0,
        "zero hallucinated run ids / out-of-vocabulary actions": (
            report.intervention_hallucination_count == 0
        ),
        "zero less-conservative disagreements": (
            report.intervention_less_conservative_count == 0
        ),
        "zero wrong-run disagreements": report.intervention_wrong_run_count == 0,
        f"more-conservative disagreement rate <= {MAX_MORE_CONSERVATIVE_RATE:.0%}": (
            more_conservative_rate <= MAX_MORE_CONSERVATIVE_RATE
        ),
        f"p95 latency <= {MAX_LATENCY_P95_SECONDS:.2f}s": (
            report.latency_p95_seconds <= MAX_LATENCY_P95_SECONDS
        ),
    }
    return GateResult(
        passed=all(checks.values()),
        checks=checks,
        latency_threshold_seconds=MAX_LATENCY_P95_SECONDS,
    )


def title_gate_result(report: ExtractionReport) -> GateResult:
    """Reported, secondary — see module docstring."""
    checks = {
        f"schema-valid rate >= {MIN_TITLE_SCHEMA_VALID_RATE:.0%}": (
            report.title_schema_valid_rate >= MIN_TITLE_SCHEMA_VALID_RATE
        ),
        f"p95 latency <= {MAX_LATENCY_P95_SECONDS:.2f}s": (
            report.latency_p95_seconds <= MAX_LATENCY_P95_SECONDS
        ),
    }
    return GateResult(
        passed=all(checks.values()),
        checks=checks,
        latency_threshold_seconds=MAX_LATENCY_P95_SECONDS,
    )


def failure_gate_result(report: ExtractionReport) -> GateResult:
    """Reported, secondary — see module docstring."""
    checks = {
        "zero parse-fallback rate": report.failure_parse_fallback_rate == 0.0,
        f"cause accuracy >= {MIN_FAILURE_CAUSE_ACCURACY:.0%}": (
            report.failure_cause_accuracy >= MIN_FAILURE_CAUSE_ACCURACY
        ),
        f"evidence accuracy >= {MIN_FAILURE_EVIDENCE_ACCURACY:.0%}": (
            report.failure_evidence_accuracy >= MIN_FAILURE_EVIDENCE_ACCURACY
        ),
        f"p95 latency <= {MAX_LATENCY_P95_SECONDS:.2f}s": (
            report.latency_p95_seconds <= MAX_LATENCY_P95_SECONDS
        ),
    }
    return GateResult(
        passed=all(checks.values()),
        checks=checks,
        latency_threshold_seconds=MAX_LATENCY_P95_SECONDS,
    )


def extraction_report_to_json(
    report: ExtractionReport,
    *,
    intervention_gate: GateResult,
    title_gate: GateResult,
    failure_gate: GateResult,
) -> str:
    """Machine-readable form — every field `extraction_report_to_markdown` renders,
    plus the raw per-fixture results each job's own markdown table only summarizes.
    """
    payload = {
        # See `heart.eval.report.report_to_json`'s matching comment — the
        # discriminator between this family and the other three under the same
        # `kind=bench/` prefix.
        "suite": "extraction",
        "model_repo": report.model_repo,
        "prompt_variant": report.prompt_variant,
        "chat_template": report.chat_template,
        "generated_at": report.generated_at,
        "host": report.host,
        "commit": report.commit,
        "branch": report.branch,
        "mlx_lm_version": report.mlx_lm_version,
        "max_output_tokens": report.max_output_tokens,
        "metrics": {
            "latency_p50_seconds": report.latency_p50_seconds,
            "latency_p95_seconds": report.latency_p95_seconds,
            "prompt_tokens_min": report.prompt_tokens_min,
            "prompt_tokens_median": report.prompt_tokens_median,
            "prompt_tokens_max": report.prompt_tokens_max,
            "peak_rss_bytes": report.peak_rss_bytes,
            "intervention": {
                "fixture_count": len(report.intervention_results),
                "schema_valid_rate": report.intervention_schema_valid_rate,
                "hallucination_count": report.intervention_hallucination_count,
                "agreement_rate": report.intervention_agreement_rate,
                "more_conservative_count": (
                    report.intervention_more_conservative_count
                ),
                "less_conservative_count": (
                    report.intervention_less_conservative_count
                ),
                "wrong_run_count": report.intervention_wrong_run_count,
                "parse_fallback_rate": report.intervention_parse_fallback_rate,
            },
            "title": {
                "fixture_count": len(report.title_results),
                "schema_valid_rate": report.title_schema_valid_rate,
                "parse_fallback_rate": report.title_parse_fallback_rate,
            },
            "failure_cause": {
                "fixture_count": len(report.failure_results),
                "schema_valid_rate": report.failure_schema_valid_rate,
                "cause_accuracy": report.failure_cause_accuracy,
                "evidence_accuracy": report.failure_evidence_accuracy,
                "parse_fallback_rate": report.failure_parse_fallback_rate,
            },
        },
        "gate": {
            "intervention": {
                "passed": intervention_gate.passed,
                "checks": intervention_gate.checks,
            },
            "title": {"passed": title_gate.passed, "checks": title_gate.checks},
            "failure_cause": {
                "passed": failure_gate.passed,
                "checks": failure_gate.checks,
            },
            "note": (
                "Only 'intervention' governs the adoption decision — a pass on "
                "'title'/'failure_cause' never licenses swapping the intervention "
                "policy."
            ),
        },
        "intervention_results": [
            {
                "id": r.fixture_id,
                "expected_action": (
                    r.expected_action.value if r.expected_action is not None else None
                ),
                "expected_run_id": r.expected_run_id,
                "actual_action": (
                    r.actual_action.value if r.actual_action is not None else None
                ),
                "actual_run_id": r.actual_run_id,
                "schema_valid": r.schema_valid,
                "direction": r.direction,
                "raw_output": r.raw_output,
                "prompt_tokens": r.prompt_tokens,
                "tags": sorted(r.tags),
            }
            for r in report.intervention_results
        ],
        "title_results": [
            {
                "id": r.fixture_id,
                "expected_title": r.expected_title,
                "actual_title": r.actual_title,
                "schema_valid": r.schema_valid,
                "raw_output": r.raw_output,
                "prompt_tokens": r.prompt_tokens,
                "tags": sorted(r.tags),
            }
            for r in report.title_results
        ],
        "failure_results": [
            {
                "id": r.fixture_id,
                "expected_cause": r.expected_cause.value,
                "expected_evidence_seq": r.expected_evidence_seq,
                "actual_cause": (
                    r.actual_cause.value if r.actual_cause is not None else None
                ),
                "actual_evidence_seq": r.actual_evidence_seq,
                "schema_valid": r.schema_valid,
                "cause_correct": r.cause_correct,
                "evidence_correct": r.evidence_correct,
                "raw_output": r.raw_output,
                "prompt_tokens": r.prompt_tokens,
                "tags": sorted(r.tags),
            }
            for r in report.failure_results
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def _render_actual_action(result: InterventionFixtureResult) -> str:
    """`result.actual_action is None` is ambiguous on its own — it means either a
    real, schema-valid "no action needed" answer (`schema_valid=True`) or a parse
    fallback (`schema_valid=False`); `"(fallback)"` must render only the latter,
    never the former, or a reader would misread a correct "none" verdict as a
    failed reply.
    """
    if result.actual_action is not None:
        return result.actual_action.value
    if result.schema_valid:
        return "none"
    return "(fallback)"


def _render_gate_section(title: str, gate: GateResult) -> list[str]:
    lines = [
        f"## {title}: {'PASS' if gate.passed else 'FAIL'}",
        "",
        "| Check | Result |",
        "|---|---|",
    ]
    lines.extend(
        f"| {name} | {'✅ pass' if ok else '❌ fail'} |"
        for name, ok in gate.checks.items()
    )
    lines.append("")
    return lines


def extraction_report_to_markdown(
    report: ExtractionReport,
    *,
    intervention_gate: GateResult,
    title_gate: GateResult,
    failure_gate: GateResult,
) -> str:
    lines = [
        f"# Heart structured-extraction bench: {report.model_repo} / "
        f"{report.prompt_variant}",
        "",
        f"- Generated: {report.generated_at}",
        f"- Host: {report.host}",
        f"- Commit: {report.commit or 'unknown'}",
        f"- Branch: {report.branch or 'unknown'}",
        f"- mlx-lm: {report.mlx_lm_version or 'unknown'}",
        f"- Chat template applied: {report.chat_template}",
        f"- Max output tokens: {report.max_output_tokens}",
        f"- Fixtures: {len(report.intervention_results)} intervention, "
        f"{len(report.title_results)} title, {len(report.failure_results)} "
        "failure-cause",
        "",
        "**Only the Intervention gate below governs the policy-swap decision** — a "
        "pass on Title/Failure-cause is reported for context only.",
        "",
    ]
    lines.extend(_render_gate_section("Intervention gate", intervention_gate))
    lines.extend(_render_gate_section("Title gate (informational)", title_gate))
    lines.extend(
        _render_gate_section("Failure-cause gate (informational)", failure_gate)
    )
    lines.extend(
        [
            "## Metrics",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Intervention schema-valid rate | "
            f"{report.intervention_schema_valid_rate:.2%} |",
            f"| Intervention agreement rate | "
            f"{report.intervention_agreement_rate:.2%} |",
            f"| Intervention more-conservative / less-conservative / wrong-run | "
            f"{report.intervention_more_conservative_count} / "
            f"{report.intervention_less_conservative_count} / "
            f"{report.intervention_wrong_run_count} |",
            f"| Title schema-valid rate | {report.title_schema_valid_rate:.2%} |",
            f"| Failure-cause schema-valid rate | "
            f"{report.failure_schema_valid_rate:.2%} |",
            f"| Failure cause accuracy | {report.failure_cause_accuracy:.2%} |",
            f"| Failure evidence accuracy | {report.failure_evidence_accuracy:.2%} |",
            f"| p50 latency | {report.latency_p50_seconds:.3f}s |",
            f"| p95 latency | {report.latency_p95_seconds:.3f}s |",
            f"| Prompt tokens (min/median/max) | "
            f"{report.prompt_tokens_min} / {report.prompt_tokens_median:.0f} / "
            f"{report.prompt_tokens_max} |",
            f"| Peak RSS | {report.peak_rss_bytes / (1024**3):.2f} GiB |",
            "",
            "## Intervention results",
            "",
            "| id | expected | actual | direction | tokens |",
            "|---|---|---|---|---|",
        ]
    )
    lines.extend(
        f"| {r.fixture_id} | "
        f"{r.expected_action.value if r.expected_action is not None else 'none'}"
        f"/{r.expected_run_id or '-'} | "
        f"{_render_actual_action(r)}"
        f"/{r.actual_run_id or '-'} | {r.direction or '-'} | {r.prompt_tokens} |"
        for r in report.intervention_results
    )
    lines.extend(
        [
            "",
            "## Title results (human sanity-check: expected vs. actual)",
            "",
            "| id | schema valid | expected | actual |",
            "|---|---|---|---|",
        ]
    )
    lines.extend(
        f"| {r.fixture_id} | {'✅' if r.schema_valid else '❌'} | "
        f"{r.expected_title} | {r.actual_title or '(fallback)'} |"
        for r in report.title_results
    )
    lines.extend(
        [
            "",
            "## Failure-cause results",
            "",
            "| id | expected cause | actual cause | cause correct | evidence correct |",
            "|---|---|---|---|---|",
        ]
    )
    lines.extend(
        f"| {r.fixture_id} | {r.expected_cause.value} | "
        f"{r.actual_cause.value if r.actual_cause is not None else '(fallback)'} | "
        f"{'✅' if r.cause_correct else '❌'} | "
        f"{'✅' if r.evidence_correct else '❌'} |"
        for r in report.failure_results
    )
    lines.append("")
    return "\n".join(lines)
