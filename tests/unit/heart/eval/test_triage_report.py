from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from ansina.heart.eval.fixtures import TriageFixture
from ansina.heart.eval.triage_report import (
    MAX_LATENCY_P95_SECONDS,
    MIN_TRIVIAL_RECALL,
    triage_gate_result,
    triage_report_to_json,
    triage_report_to_markdown,
)
from ansina.heart.eval.triage_runner import TriageReport, run_triage_bench
from ansina.heart.triage import RequestTriage, TriageClass, TriageOutcome


class _ScriptedTriage(RequestTriage):
    def __init__(self, *, replies: Sequence[TriageClass | None]) -> None:
        self._replies = list(replies)
        self.calls = 0

    def classify(self, request: str) -> TriageOutcome:
        index = min(self.calls, len(self._replies) - 1)
        self.calls += 1
        parsed = self._replies[index]
        return TriageOutcome(
            request=request,
            triage_class=parsed if parsed is not None else TriageClass.COMPLEX,
            parsed=parsed,
            raw_output=parsed.value if parsed is not None else "???",
            prompt_tokens=10,
        )


def _fixture(fixture_id: str, expect: TriageClass) -> TriageFixture:
    return TriageFixture(id=fixture_id, expect=expect, request="r")


_ALL_CORRECT_FIXTURES = [
    _fixture("t1", TriageClass.TRIVIAL),
    _fixture("o1", TriageClass.TOOL_ONLY),
    _fixture("c1", TriageClass.COMPLEX),
]
_ALL_CORRECT_REPLIES = [TriageClass.TRIVIAL, TriageClass.TOOL_ONLY, TriageClass.COMPLEX]


def _report(
    fixtures: Sequence[TriageFixture] = _ALL_CORRECT_FIXTURES,
    replies: Sequence[TriageClass | None] = _ALL_CORRECT_REPLIES,
) -> TriageReport:
    triage = _ScriptedTriage(replies=replies)
    return run_triage_bench(
        triage,
        fixtures,
        model_repo="mlx-community/Test-Model-4bit",
        prompt_variant="strict",
        max_output_tokens=50,
        commit="abc1234",
        branch="m7-first-brain",
    )


def test_gate_passes_when_every_criterion_clears() -> None:
    result = triage_gate_result(_report())

    assert result.passed is True
    assert all(result.checks.values())
    assert result.latency_threshold_seconds == pytest.approx(MAX_LATENCY_P95_SECONDS)


def test_gate_fails_on_a_complex_to_trivial_misroute() -> None:
    result = triage_gate_result(
        _report(
            fixtures=[_fixture("c1", TriageClass.COMPLEX)],
            replies=[TriageClass.TRIVIAL],
        )
    )

    assert result.passed is False
    assert result.checks["zero complex->trivial misroutes"] is False


def test_gate_fails_on_a_tool_only_to_trivial_misroute() -> None:
    result = triage_gate_result(
        _report(
            fixtures=[_fixture("o1", TriageClass.TOOL_ONLY)],
            replies=[TriageClass.TRIVIAL],
        )
    )

    assert result.passed is False
    assert result.checks["zero tool-only->trivial misroutes"] is False


def test_gate_fails_on_low_trivial_recall() -> None:
    # 3 trivial fixtures, 2 over-routed to complex -> trivial recall 1/3 < 0.75.
    result = triage_gate_result(
        _report(
            fixtures=[
                _fixture("t1", TriageClass.TRIVIAL),
                _fixture("t2", TriageClass.TRIVIAL),
                _fixture("t3", TriageClass.TRIVIAL),
            ],
            replies=[TriageClass.TRIVIAL, TriageClass.COMPLEX, TriageClass.COMPLEX],
        )
    )

    assert result.passed is False
    assert result.checks[f"trivial recall >= {MIN_TRIVIAL_RECALL:.0%}"] is False


def test_gate_fails_on_a_parse_fallback() -> None:
    result = triage_gate_result(
        _report(fixtures=[_fixture("t1", TriageClass.TRIVIAL)], replies=[None])
    )

    assert result.passed is False
    assert result.checks["zero parse-fallback rate"] is False


def test_triage_report_to_json_round_trips_through_json_loads() -> None:
    report = _report()
    gate = triage_gate_result(report)

    text = triage_report_to_json(report, gate=gate)
    payload = json.loads(text)

    assert payload["suite"] == "triage"
    assert payload["model_repo"] == "mlx-community/Test-Model-4bit"
    assert payload["prompt_variant"] == "strict"
    assert payload["commit"] == "abc1234"
    assert payload["branch"] == "m7-first-brain"
    assert payload["metrics"]["accuracy"] == 1.0
    assert payload["metrics"]["recall_by_class"] == {
        "trivial": 1.0,
        "tool-only": 1.0,
        "complex": 1.0,
    }
    assert payload["metrics"]["brain_calls_saved_fraction"] == pytest.approx(1 / 3)
    assert payload["metrics"]["confusion"]["trivial"]["trivial"] == 1
    assert payload["gate"]["passed"] is True
    assert payload["results"][0]["routed_to_brain"] is False


def test_triage_report_to_json_renders_a_parse_fallback_as_null_actual() -> None:
    report = _report(fixtures=[_fixture("c1", TriageClass.COMPLEX)], replies=[None])
    gate = triage_gate_result(report)

    payload = json.loads(triage_report_to_json(report, gate=gate))

    assert payload["results"][0]["actual"] is None
    assert payload["results"][0]["parse_fallback"] is True


def test_triage_report_to_markdown_shows_pass_and_every_section() -> None:
    report = _report()
    gate = triage_gate_result(report)

    text = triage_report_to_markdown(report, gate=gate)

    assert "# Heart triage bench: mlx-community/Test-Model-4bit / strict" in text
    assert "- Commit: abc1234" in text
    assert "- Branch: m7-first-brain" in text
    assert "## Gate: PASS" in text
    assert "## Metrics" in text
    assert "## Per-class recall" in text
    assert "## Recall by tag" in text
    assert "## Confusion matrix" in text
    assert "## Per-fixture results" in text


def test_triage_report_to_markdown_shows_fail_and_a_fallback_row() -> None:
    report = _report(fixtures=[_fixture("c1", TriageClass.COMPLEX)], replies=[None])
    gate = triage_gate_result(report)

    text = triage_report_to_markdown(report, gate=gate)

    assert "## Gate: FAIL" in text
    assert "(fallback)" in text
    assert "❌ fail" in text


def test_triage_report_to_markdown_shows_unknown_mlx_lm_version_when_none() -> None:
    report = _report()
    object.__setattr__(report, "mlx_lm_version", None)
    gate = triage_gate_result(report)

    text = triage_report_to_markdown(report, gate=gate)

    assert "mlx-lm: unknown" in text


def test_triage_report_to_markdown_shows_unknown_commit_and_branch_when_none() -> None:
    report = _report(
        fixtures=[_fixture("t1", TriageClass.TRIVIAL)], replies=[TriageClass.TRIVIAL]
    )
    object.__setattr__(report, "commit", None)
    object.__setattr__(report, "branch", None)
    gate = triage_gate_result(report)

    text = triage_report_to_markdown(report, gate=gate)

    assert "- Commit: unknown" in text
    assert "- Branch: unknown" in text


def test_triage_report_to_json_renders_commit_and_branch_as_null_when_none() -> None:
    report = _report()
    object.__setattr__(report, "commit", None)
    object.__setattr__(report, "branch", None)
    gate = triage_gate_result(report)

    payload = json.loads(triage_report_to_json(report, gate=gate))

    assert payload["commit"] is None
    assert payload["branch"] is None
