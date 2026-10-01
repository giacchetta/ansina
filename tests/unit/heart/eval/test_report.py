from __future__ import annotations

import json
from types import MappingProxyType

import pytest

from ansina.heart.eval.report import gate_result, report_to_json, report_to_markdown
from ansina.heart.eval.runner import BenchReport, FixtureResult
from ansina.heart.tick.decision import TickDecision


def _report(
    *,
    accuracy: float = 1.0,
    false_act_or_escalate_on_obvious_idle: int = 0,
    parse_fallback_rate: float = 0.0,
    latency_p95_seconds: float = 1.0,
    commit: str | None = "abc1234",
    branch: str | None = "m6-heartbeat",
    results: tuple[FixtureResult, ...] | None = None,
) -> BenchReport:
    if results is None:
        results = (
            FixtureResult(
                fixture_id="i1",
                expected=TickDecision.IDLE,
                actual=TickDecision.IDLE,
                raw_output="idle",
                latency_seconds=0.5,
                prompt_tokens=100,
                tags=frozenset({"obviously_idle"}),
            ),
        )
    return BenchReport(
        model_repo="mlx-community/Test-Model-4bit",
        prompt_variant="baseline",
        chat_template=False,
        generated_at="2026-09-29T00:00:00+00:00",
        host="macOS-26-arm64",
        commit=commit,
        branch=branch,
        mlx_lm_version="0.31.3",
        max_output_tokens=50,
        results=results,
        accuracy=accuracy,
        recall_by_class=MappingProxyType(
            {
                TickDecision.IDLE: 1.0,
                TickDecision.ACT: 1.0,
                TickDecision.ESCALATE: 1.0,
            }
        ),
        class_counts=MappingProxyType(
            {TickDecision.IDLE: 8, TickDecision.ACT: 8, TickDecision.ESCALATE: 8}
        ),
        recall_by_tag=MappingProxyType({"obviously_idle": 1.0}),
        tag_counts=MappingProxyType({"obviously_idle": 1}),
        parse_fallback_rate=parse_fallback_rate,
        false_act_or_escalate_on_obvious_idle=false_act_or_escalate_on_obvious_idle,
        latency_p50_seconds=0.5,
        latency_p95_seconds=latency_p95_seconds,
        prompt_tokens_min=90,
        prompt_tokens_median=100.0,
        prompt_tokens_max=110,
        peak_rss_bytes=2 * 1024**3,
    )


def test_gate_passes_when_every_criterion_clears() -> None:
    result = gate_result(_report(), interval_seconds=30.0)

    assert result.passed is True
    assert all(result.checks.values())
    assert result.latency_threshold_seconds == pytest.approx(6.0)


def test_gate_fails_on_low_accuracy() -> None:
    result = gate_result(_report(accuracy=0.5), interval_seconds=30.0)

    assert result.passed is False
    assert result.checks["accuracy >= 0.90"] is False


def test_gate_fails_on_a_false_act_or_escalate_on_obviously_idle() -> None:
    result = gate_result(
        _report(false_act_or_escalate_on_obvious_idle=1), interval_seconds=30.0
    )

    assert result.passed is False
    assert result.checks["zero false act/escalate on obviously_idle fixtures"] is False


def test_gate_fails_on_nonzero_parse_fallback_rate() -> None:
    result = gate_result(_report(parse_fallback_rate=0.1), interval_seconds=30.0)

    assert result.passed is False
    assert result.checks["zero parse-fallback rate"] is False


def test_gate_fails_when_p95_latency_exceeds_20_percent_of_the_interval() -> None:
    result = gate_result(_report(latency_p95_seconds=6.01), interval_seconds=30.0)

    assert result.passed is False


def test_gate_passes_at_exactly_the_latency_threshold() -> None:
    result = gate_result(_report(latency_p95_seconds=6.0), interval_seconds=30.0)

    assert result.passed is True


def test_report_to_json_round_trips_through_json_loads() -> None:
    report = _report()
    gate = gate_result(report, interval_seconds=30.0)

    text = report_to_json(report, gate=gate)
    payload = json.loads(text)

    assert payload["model_repo"] == "mlx-community/Test-Model-4bit"
    assert payload["prompt_variant"] == "baseline"
    assert payload["commit"] == "abc1234"
    assert payload["branch"] == "m6-heartbeat"
    assert payload["metrics"]["accuracy"] == 1.0
    assert payload["metrics"]["recall_by_class"] == {
        "idle": 1.0,
        "act": 1.0,
        "escalate": 1.0,
    }
    assert payload["metrics"]["recall_by_tag"] == {"obviously_idle": 1.0}
    assert payload["metrics"]["tag_counts"] == {"obviously_idle": 1}
    assert payload["gate"]["passed"] is True
    assert payload["results"][0]["id"] == "i1"
    assert payload["results"][0]["actual"] == "idle"
    assert payload["results"][0]["tags"] == ["obviously_idle"]


def test_report_to_json_renders_a_parse_fallback_as_null_actual() -> None:
    results = (
        FixtureResult(
            fixture_id="x",
            expected=TickDecision.IDLE,
            actual=None,
            raw_output="???",
            latency_seconds=0.1,
            prompt_tokens=10,
            tags=frozenset(),
        ),
    )
    report = _report(results=results, accuracy=0.0, parse_fallback_rate=1.0)
    gate = gate_result(report, interval_seconds=30.0)

    payload = json.loads(report_to_json(report, gate=gate))

    assert payload["results"][0]["actual"] is None
    assert payload["results"][0]["parse_fallback"] is True


def test_report_to_markdown_shows_pass_and_every_section() -> None:
    report = _report()
    gate = gate_result(report, interval_seconds=30.0)

    text = report_to_markdown(report, gate=gate)

    assert "# Heart bench: mlx-community/Test-Model-4bit / baseline" in text
    assert "- Commit: abc1234" in text
    assert "- Branch: m6-heartbeat" in text
    assert "## Gate: PASS" in text
    assert "## Metrics" in text
    assert "## Per-class recall" in text
    assert "## Recall by tag" in text
    assert "| obviously_idle | 100.00% | 1 |" in text
    assert "## Per-fixture results" in text
    assert "i1" in text
    assert "idle" in text


def test_report_to_markdown_shows_fail_and_a_fallback_row() -> None:
    results = (
        FixtureResult(
            fixture_id="x",
            expected=TickDecision.IDLE,
            actual=None,
            raw_output="???",
            latency_seconds=0.1,
            prompt_tokens=10,
            tags=frozenset(),
        ),
    )
    report = _report(results=results, accuracy=0.0, parse_fallback_rate=1.0)
    gate = gate_result(report, interval_seconds=30.0)

    text = report_to_markdown(report, gate=gate)

    assert "## Gate: FAIL" in text
    assert "(fallback)" in text
    assert "❌ fail" in text


def test_report_to_markdown_shows_unknown_mlx_lm_version_when_none() -> None:
    report = _report()
    object.__setattr__(report, "mlx_lm_version", None)
    gate = gate_result(report, interval_seconds=30.0)

    text = report_to_markdown(report, gate=gate)

    assert "mlx-lm: unknown" in text


def test_report_to_markdown_shows_unknown_commit_and_branch_when_none() -> None:
    report = _report(commit=None, branch=None)
    gate = gate_result(report, interval_seconds=30.0)

    text = report_to_markdown(report, gate=gate)

    assert "- Commit: unknown" in text
    assert "- Branch: unknown" in text


def test_report_to_json_renders_commit_and_branch_as_null_when_none() -> None:
    report = _report(commit=None, branch=None)
    gate = gate_result(report, interval_seconds=30.0)

    payload = json.loads(report_to_json(report, gate=gate))

    assert payload["commit"] is None
    assert payload["branch"] is None
