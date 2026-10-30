from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Sequence

from ansina.heart.curation import CurationEvent
from ansina.heart.eval.extraction_report import (
    MAX_LATENCY_P95_SECONDS,
    extraction_report_to_json,
    extraction_report_to_markdown,
    failure_gate_result,
    intervention_gate_result,
    title_gate_result,
)
from ansina.heart.eval.extraction_runner import ExtractionReport, run_extraction_bench
from ansina.heart.eval.fixtures import (
    ExtractionFixture,
    FailureExtractionFixture,
    InterventionExtractionFixture,
    TitleExtractionFixture,
)
from ansina.heart.extraction import (
    FailureCause,
    FailureExtraction,
    FailureExtractionOutcome,
    InterventionCandidateRun,
    InterventionExtraction,
    InterventionExtractionOutcome,
    TitleExtractionOutcome,
)
from ansina.opencode.interventions import OpencodeAction


def _event(sequence: int = 0, *, payload: str = "p") -> CurationEvent:
    return CurationEvent(sequence=sequence, raw_type="text", payload=payload)


def _intervention_fixture() -> InterventionExtractionFixture:
    return InterventionExtractionFixture(
        id="i1",
        candidates=(
            InterventionCandidateRun(run_id="r1", status="failed", detail="d"),
        ),
        intake_paused=False,
        recent_failed=1,
        recent_timeout=0,
        recent_permission_denied=0,
        expected_action=OpencodeAction.RETRY,
        expected_run_id="r1",
    )


def _title_fixture() -> TitleExtractionFixture:
    return TitleExtractionFixture(
        id="t1", prompt="do it", events=(_event(0),), expected_title="A title"
    )


def _failure_fixture() -> FailureExtractionFixture:
    return FailureExtractionFixture(
        id="f1",
        events=(_event(0),),
        expected_cause=FailureCause.SHELL_DENIED,
        expected_evidence_seq=0,
    )


_DEFAULT_INTERVENTION = InterventionExtraction(
    action=OpencodeAction.RETRY, run_id="r1", reason="x"
)
_DEFAULT_FAILURE = FailureExtraction(
    cause=FailureCause.SHELL_DENIED, evidence_event_seq=0
)


class _ScriptedExtractor:
    def __init__(
        self,
        *,
        intervention: InterventionExtraction | None = _DEFAULT_INTERVENTION,
        title: str | None = "A title",
        failure: FailureExtraction | None = _DEFAULT_FAILURE,
    ) -> None:
        self._intervention = intervention
        self._title = title
        self._failure = failure

    def extract_intervention(
        self,
        candidates: Sequence[InterventionCandidateRun],
        *,
        intake_paused: bool,
        recent_failed: int,
        recent_timeout: int,
        recent_permission_denied: int,
    ) -> InterventionExtractionOutcome:
        return InterventionExtractionOutcome(
            candidates=tuple(candidates),
            parsed=self._intervention,
            raw_output="scripted",
            prompt_tokens=10,
        )

    def extract_title(
        self, prompt: str, events: Sequence[CurationEvent]
    ) -> TitleExtractionOutcome:
        return TitleExtractionOutcome(
            prompt=prompt, parsed=self._title, raw_output="scripted", prompt_tokens=10
        )

    def extract_failure_cause(
        self, events: Sequence[CurationEvent]
    ) -> FailureExtractionOutcome:
        return FailureExtractionOutcome(
            events=tuple(events),
            parsed=self._failure,
            raw_output="scripted",
            prompt_tokens=10,
        )


def _report(
    *,
    fixtures: Sequence[ExtractionFixture] | None = None,
    extractor: _ScriptedExtractor | None = None,
    perf_counter: Callable[[], float] = lambda: 0.0,
) -> ExtractionReport:
    """Builds a report scored as a PASS by every gate by default — every job's
    scripted outcome agrees with its fixture's own ground truth. Callers override
    `fixtures`/`extractor`/`perf_counter` to exercise a specific FAIL branch.
    """
    resolved_fixtures: Sequence[ExtractionFixture] = (
        fixtures
        if fixtures is not None
        else [_intervention_fixture(), _title_fixture(), _failure_fixture()]
    )
    return run_extraction_bench(
        extractor or _ScriptedExtractor(),
        resolved_fixtures,
        model_repo="mlx-community/Test-Model-4bit",
        prompt_variant="strict",
        max_output_tokens=50,
        commit="abc1234",
        branch="m9-opencode-executor-hands",
        perf_counter=perf_counter,
    )


# --- intervention_gate_result --------------------------------------------------------


def test_intervention_gate_passes_when_every_criterion_clears() -> None:
    result = intervention_gate_result(_report())

    assert result.passed is True
    assert all(result.checks.values())


def test_intervention_gate_fails_on_a_parse_fallback() -> None:
    report = _report(extractor=_ScriptedExtractor(intervention=None))

    result = intervention_gate_result(report)

    assert result.passed is False
    assert result.checks["zero parse-fallback rate"] is False


def test_intervention_gate_fails_on_a_less_conservative_disagreement() -> None:
    fixture = InterventionExtractionFixture(
        id="i1",
        candidates=(
            InterventionCandidateRun(run_id="r1", status="failed", detail="d"),
        ),
        intake_paused=False,
        recent_failed=1,
        recent_timeout=0,
        recent_permission_denied=0,
        expected_action=OpencodeAction.PAUSE_INTAKE,
        expected_run_id=None,
    )
    report = _report(
        fixtures=[fixture],
        extractor=_ScriptedExtractor(
            intervention=InterventionExtraction(
                action=OpencodeAction.RETRY, run_id="r1", reason="x"
            )
        ),
    )

    result = intervention_gate_result(report)

    assert result.passed is False
    assert result.checks["zero less-conservative disagreements"] is False


def test_intervention_gate_fails_on_a_wrong_run_disagreement() -> None:
    report = _report(
        extractor=_ScriptedExtractor(
            intervention=InterventionExtraction(
                action=OpencodeAction.RETRY, run_id="other", reason="x"
            )
        )
    )

    result = intervention_gate_result(report)

    assert result.passed is False
    assert result.checks["zero wrong-run disagreements"] is False


def test_intervention_gate_fails_on_too_high_a_more_conservative_rate() -> None:
    fixture = InterventionExtractionFixture(
        id="i1",
        candidates=(
            InterventionCandidateRun(run_id="r1", status="failed", detail="d"),
        ),
        intake_paused=False,
        recent_failed=1,
        recent_timeout=0,
        recent_permission_denied=0,
        expected_action=OpencodeAction.RETRY,
        expected_run_id="r1",
    )
    report = _report(
        fixtures=[fixture],
        extractor=_ScriptedExtractor(
            intervention=InterventionExtraction(
                action=OpencodeAction.ESCALATE_TO_BRAIN, run_id=None, reason="x"
            )
        ),
    )

    result = intervention_gate_result(report)

    assert result.passed is False
    assert any("more-conservative disagreement rate" in name for name in result.checks)
    assert not all(result.checks.values())


def test_intervention_gate_fails_on_too_high_a_latency() -> None:
    report = _report(
        perf_counter=itertools.cycle([0.0, MAX_LATENCY_P95_SECONDS + 1]).__next__
    )

    result = intervention_gate_result(report)

    assert result.passed is False
    assert any("p95 latency" in name and not ok for name, ok in result.checks.items())


# --- title_gate_result ----------------------------------------------------------------


def test_title_gate_passes_when_every_criterion_clears() -> None:
    result = title_gate_result(_report())

    assert result.passed is True
    assert all(result.checks.values())


def test_title_gate_fails_on_a_low_schema_valid_rate() -> None:
    report = _report(extractor=_ScriptedExtractor(title=None))

    result = title_gate_result(report)

    assert result.passed is False
    assert not all(result.checks.values())


def test_title_gate_fails_on_too_high_a_latency() -> None:
    report = _report(
        perf_counter=itertools.cycle([0.0, MAX_LATENCY_P95_SECONDS + 1]).__next__
    )

    result = title_gate_result(report)

    assert result.passed is False


# --- failure_gate_result --------------------------------------------------------------


def test_failure_gate_passes_when_every_criterion_clears() -> None:
    result = failure_gate_result(_report())

    assert result.passed is True
    assert all(result.checks.values())


def test_failure_gate_fails_on_a_parse_fallback() -> None:
    report = _report(extractor=_ScriptedExtractor(failure=None))

    result = failure_gate_result(report)

    assert result.passed is False
    assert result.checks["zero parse-fallback rate"] is False


def test_failure_gate_fails_on_a_wrong_cause() -> None:
    report = _report(
        extractor=_ScriptedExtractor(
            failure=FailureExtraction(cause=FailureCause.OTHER, evidence_event_seq=0)
        )
    )

    result = failure_gate_result(report)

    assert result.passed is False
    assert not all(result.checks.values())


def test_failure_gate_fails_on_wrong_evidence() -> None:
    fixture = FailureExtractionFixture(
        id="f1",
        events=(_event(0), _event(1)),
        expected_cause=FailureCause.SHELL_DENIED,
        expected_evidence_seq=1,
    )
    report = _report(
        fixtures=[fixture],
        extractor=_ScriptedExtractor(
            failure=FailureExtraction(
                cause=FailureCause.SHELL_DENIED, evidence_event_seq=0
            )
        ),
    )

    result = failure_gate_result(report)

    assert result.passed is False
    assert not all(result.checks.values())


def test_failure_gate_fails_on_too_high_a_latency() -> None:
    report = _report(
        perf_counter=itertools.cycle([0.0, MAX_LATENCY_P95_SECONDS + 1]).__next__
    )

    result = failure_gate_result(report)

    assert result.passed is False


# --- extraction_report_to_json / _to_markdown --------------------------------------


def test_extraction_report_to_json_round_trips_through_json_loads() -> None:
    report = _report()
    intervention_gate = intervention_gate_result(report)
    title_gate = title_gate_result(report)
    failure_gate = failure_gate_result(report)

    payload = json.loads(
        extraction_report_to_json(
            report,
            intervention_gate=intervention_gate,
            title_gate=title_gate,
            failure_gate=failure_gate,
        )
    )

    assert payload["suite"] == "extraction"
    assert payload["model_repo"] == "mlx-community/Test-Model-4bit"
    assert payload["commit"] == "abc1234"
    assert payload["branch"] == "m9-opencode-executor-hands"
    assert payload["gate"]["intervention"]["passed"] is True
    assert payload["gate"]["title"]["passed"] is True
    assert payload["gate"]["failure_cause"]["passed"] is True
    assert "note" in payload["gate"]
    assert payload["intervention_results"][0]["id"] == "i1"
    assert payload["intervention_results"][0]["expected_action"] == "retry"
    assert payload["title_results"][0]["id"] == "t1"
    assert payload["failure_results"][0]["id"] == "f1"
    assert payload["failure_results"][0]["expected_cause"] == "shell_denied"


def test_extraction_report_to_json_renders_null_action_and_null_commit() -> None:
    fixture = InterventionExtractionFixture(
        id="i1",
        candidates=(
            InterventionCandidateRun(run_id="r1", status="running", detail="d"),
        ),
        intake_paused=False,
        recent_failed=0,
        recent_timeout=0,
        recent_permission_denied=0,
        expected_action=None,
        expected_run_id=None,
    )
    report = _report(
        fixtures=[fixture],
        extractor=_ScriptedExtractor(
            intervention=InterventionExtraction(action=None, run_id=None, reason="")
        ),
    )
    object.__setattr__(report, "commit", None)
    object.__setattr__(report, "branch", None)
    intervention_gate = intervention_gate_result(report)
    title_gate = title_gate_result(report)
    failure_gate = failure_gate_result(report)

    payload = json.loads(
        extraction_report_to_json(
            report,
            intervention_gate=intervention_gate,
            title_gate=title_gate,
            failure_gate=failure_gate,
        )
    )

    assert payload["commit"] is None
    assert payload["branch"] is None
    assert payload["intervention_results"][0]["expected_action"] is None
    assert payload["intervention_results"][0]["actual_action"] is None


def test_extraction_report_to_markdown_distinguishes_none_from_a_fallback() -> None:
    """A schema-valid `"no action needed"` answer (`action=None` but
    `schema_valid=True`) must render as `none`, never `(fallback)` — the latter
    is reserved for a genuine parse failure (`schema_valid=False`), per a real
    Mac Mini M4 bench run that found this exact rendering ambiguous.
    """
    fixture = InterventionExtractionFixture(
        id="i1",
        candidates=(
            InterventionCandidateRun(run_id="r1", status="running", detail="d"),
        ),
        intake_paused=False,
        recent_failed=0,
        recent_timeout=0,
        recent_permission_denied=0,
        expected_action=None,
        expected_run_id=None,
    )
    report = _report(
        fixtures=[fixture],
        extractor=_ScriptedExtractor(
            intervention=InterventionExtraction(action=None, run_id=None, reason="")
        ),
    )
    intervention_gate = intervention_gate_result(report)
    title_gate = title_gate_result(report)
    failure_gate = failure_gate_result(report)

    text = extraction_report_to_markdown(
        report,
        intervention_gate=intervention_gate,
        title_gate=title_gate,
        failure_gate=failure_gate,
    )

    assert "| i1 | none/- | none/- | agree |" in text
    assert "(fallback)" not in text


def test_extraction_report_to_markdown_shows_pass_and_every_section() -> None:
    report = _report()
    intervention_gate = intervention_gate_result(report)
    title_gate = title_gate_result(report)
    failure_gate = failure_gate_result(report)

    text = extraction_report_to_markdown(
        report,
        intervention_gate=intervention_gate,
        title_gate=title_gate,
        failure_gate=failure_gate,
    )

    assert (
        "# Heart structured-extraction bench: mlx-community/Test-Model-4bit / strict"
        in text
    )
    assert "- Commit: abc1234" in text
    assert "- Branch: m9-opencode-executor-hands" in text
    assert "## Intervention gate: PASS" in text
    assert "## Title gate (informational): PASS" in text
    assert "## Failure-cause gate (informational): PASS" in text
    assert "## Metrics" in text
    assert "## Intervention results" in text
    assert "## Title results" in text
    assert "## Failure-cause results" in text


def test_extraction_report_to_markdown_shows_fail_and_a_failing_row() -> None:
    report = _report(extractor=_ScriptedExtractor(intervention=None, title=None))
    intervention_gate = intervention_gate_result(report)
    title_gate = title_gate_result(report)
    failure_gate = failure_gate_result(report)

    text = extraction_report_to_markdown(
        report,
        intervention_gate=intervention_gate,
        title_gate=title_gate,
        failure_gate=failure_gate,
    )

    assert "## Intervention gate: FAIL" in text
    assert "## Title gate (informational): FAIL" in text
    assert "❌ fail" in text
    assert "(fallback)" in text


def test_extraction_report_to_markdown_shows_unknown_mlx_lm_version() -> None:
    report = _report()
    object.__setattr__(report, "mlx_lm_version", None)
    intervention_gate = intervention_gate_result(report)
    title_gate = title_gate_result(report)
    failure_gate = failure_gate_result(report)

    text = extraction_report_to_markdown(
        report,
        intervention_gate=intervention_gate,
        title_gate=title_gate,
        failure_gate=failure_gate,
    )

    assert "mlx-lm: unknown" in text


def test_extraction_report_to_markdown_shows_unknown_commit_and_branch() -> None:
    report = _report()
    object.__setattr__(report, "commit", None)
    object.__setattr__(report, "branch", None)
    intervention_gate = intervention_gate_result(report)
    title_gate = title_gate_result(report)
    failure_gate = failure_gate_result(report)

    text = extraction_report_to_markdown(
        report,
        intervention_gate=intervention_gate,
        title_gate=title_gate,
        failure_gate=failure_gate,
    )

    assert "- Commit: unknown" in text
    assert "- Branch: unknown" in text
