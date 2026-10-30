from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Sequence

from ansina.brain.events import BrainDone, BrainEvent, BrainTextDelta
from ansina.brain.provider import BrainRequest
from ansina.heart.curation import (
    CurationEvent,
    CurationOutcome,
    recency_window_selection,
)
from ansina.heart.eval.curation_report import (
    curation_gate_result,
    curation_report_to_json,
    curation_report_to_markdown,
)
from ansina.heart.eval.curation_runner import CurationReport, run_curation_bench
from ansina.heart.eval.fixtures import CurationFixture


def _event(sequence: int, *, raw_type: str = "t", payload: str = "p") -> CurationEvent:
    return CurationEvent(sequence=sequence, raw_type=raw_type, payload=payload)


def _fixture(fixture_id: str, *, failure_cause: str = "root cause") -> CurationFixture:
    events = (
        _event(0, raw_type="run_event", payload="root cause here"),
        _event(1, raw_type="run_event", payload="later unrelated event"),
    )
    return CurationFixture(
        id=fixture_id,
        events=events,
        must_keep=frozenset({0}),
        failure_cause=failure_cause,
    )


class _ScriptedCurator:
    def __init__(self, *, selections: Sequence[frozenset[int] | None]) -> None:
        self._selections = list(selections)
        self.calls = 0

    def curate(
        self, events: Sequence[CurationEvent], *, budget_tokens: int
    ) -> CurationOutcome:
        index = min(self.calls, len(self._selections) - 1)
        self.calls += 1
        parsed = self._selections[index]
        selected = (
            parsed
            if parsed is not None
            else recency_window_selection(
                events, budget_tokens=budget_tokens, token_count=len
            )
        )
        return CurationOutcome(
            events=tuple(events),
            selected=selected,
            parsed=parsed,
            raw_output="x",
            prompt_tokens=10,
            exceeded_budget=False,
        )


class _ScriptedBrain:
    def __init__(self, *, answers: Sequence[str]) -> None:
        self._answers = list(answers)
        self.calls = 0

    def stream(self, request: BrainRequest) -> AsyncGenerator[BrainEvent]:
        index = min(self.calls, len(self._answers) - 1)
        self.calls += 1
        answer = self._answers[index]

        async def _gen() -> AsyncGenerator[BrainEvent]:
            yield BrainTextDelta(text=answer)
            yield BrainDone()

        return _gen()

    async def aclose(self) -> None:
        pass


async def _report(
    fixtures: Sequence[CurationFixture] | None = None,
    *,
    curator_selections: Sequence[frozenset[int] | None] | None = None,
    brain_answers: Sequence[str] | None = None,
) -> CurationReport:
    """Builds a report scored as a PASS by default — `curator_selections` keeps
    every event (trivial recall), and `brain_answers` has the curated answer mention
    the real root cause while the truncated one doesn't, matching every gate check.
    Callers override either to exercise a specific FAIL branch.
    """
    resolved_fixtures = list(fixtures) if fixtures is not None else [_fixture("c1")]
    curator = _ScriptedCurator(selections=curator_selections or [frozenset({0, 1})])
    brain = _ScriptedBrain(
        answers=brain_answers
        or ["it was caused by the root cause", "a later, unrelated explanation"]
    )
    return await run_curation_bench(
        curator,
        brain,
        resolved_fixtures,
        model_repo="mlx-community/Test-Model-4bit",
        prompt_variant="strict",
        brain_model="fake-brain",
        max_output_tokens=50,
        token_count=len,
        commit="abc1234",
        branch="m9-opencode-executor-hands",
    )


async def test_gate_passes_when_every_criterion_clears() -> None:
    report = await _report()

    result = curation_gate_result(report, interval_seconds=30.0)

    assert result.passed is True
    assert all(result.checks.values())


async def test_gate_fails_when_curated_does_not_beat_truncated() -> None:
    report = await _report(brain_answers=["unrelated", "also unrelated"])

    result = curation_gate_result(report, interval_seconds=30.0)

    assert result.passed is False
    assert (
        result.checks[
            "curated beats truncated on identifying the failure cause (margin >= 1)"
        ]
        is False
    )


async def test_gate_fails_on_a_tie() -> None:
    """A tie (both conditions identify it, or neither does) rejects — truncation is
    free, per the issue's own rule.
    """
    report = await _report(
        brain_answers=[
            "it was caused by the root cause",
            "it was caused by the root cause",
        ]
    )

    result = curation_gate_result(report, interval_seconds=30.0)

    assert result.passed is False
    assert (
        result.checks[
            "curated beats truncated on identifying the failure cause (margin >= 1)"
        ]
        is False
    )


async def test_gate_fails_on_a_dropped_must_keep_event() -> None:
    report = await _report(curator_selections=[frozenset({1})])

    result = curation_gate_result(report, interval_seconds=30.0)

    assert result.passed is False
    assert result.checks["must-keep recall == 100%"] is False


async def test_gate_fails_on_a_parse_fallback() -> None:
    report = await _report(curator_selections=[None])

    result = curation_gate_result(report, interval_seconds=30.0)

    assert result.passed is False
    assert result.checks["zero parse-fallback rate"] is False


async def test_curation_report_to_json_round_trips_through_json_loads() -> None:
    report = await _report()
    gate = curation_gate_result(report, interval_seconds=30.0)

    payload = json.loads(curation_report_to_json(report, gate=gate))

    assert payload["suite"] == "curation"
    assert payload["model_repo"] == "mlx-community/Test-Model-4bit"
    assert payload["brain_model"] == "fake-brain"
    assert payload["commit"] == "abc1234"
    assert payload["branch"] == "m9-opencode-executor-hands"
    assert payload["metrics"]["must_keep_recall_rate"] == 1.0
    assert payload["metrics"]["curated_identified_count"] == 1
    assert payload["metrics"]["truncated_identified_count"] == 0
    assert payload["gate"]["passed"] is True
    assert payload["results"][0]["curated_recall"] is True


async def test_curation_report_to_json_renders_null_commit_and_branch() -> None:
    report = await _report()
    object.__setattr__(report, "commit", None)
    object.__setattr__(report, "branch", None)
    gate = curation_gate_result(report, interval_seconds=30.0)

    payload = json.loads(curation_report_to_json(report, gate=gate))

    assert payload["commit"] is None
    assert payload["branch"] is None


async def test_curation_report_to_markdown_shows_pass_and_every_section() -> None:
    report = await _report()
    gate = curation_gate_result(report, interval_seconds=30.0)

    text = curation_report_to_markdown(report, gate=gate)

    assert (
        "# Heart context-curation bench: mlx-community/Test-Model-4bit / strict" in text
    )
    assert "- Commit: abc1234" in text
    assert "- Branch: m9-opencode-executor-hands" in text
    assert "## Gate: PASS" in text
    assert "## Metrics" in text
    assert "## Per-fixture results" in text
    assert "## Per-fixture answers" in text


async def test_curation_report_to_markdown_shows_fail_and_a_failing_row() -> None:
    report = await _report(brain_answers=["unrelated", "also unrelated"])
    gate = curation_gate_result(report, interval_seconds=30.0)

    text = curation_report_to_markdown(report, gate=gate)

    assert "## Gate: FAIL" in text
    assert "❌ fail" in text


async def test_curation_report_to_markdown_shows_unknown_mlx_lm_version_when_none() -> (
    None
):
    report = await _report()
    object.__setattr__(report, "mlx_lm_version", None)
    gate = curation_gate_result(report, interval_seconds=30.0)

    text = curation_report_to_markdown(report, gate=gate)

    assert "mlx-lm: unknown" in text


async def test_curation_report_to_markdown_shows_unknown_commit_and_branch() -> None:
    report = await _report()
    object.__setattr__(report, "commit", None)
    object.__setattr__(report, "branch", None)
    gate = curation_gate_result(report, interval_seconds=30.0)

    text = curation_report_to_markdown(report, gate=gate)

    assert "- Commit: unknown" in text
    assert "- Branch: unknown" in text
