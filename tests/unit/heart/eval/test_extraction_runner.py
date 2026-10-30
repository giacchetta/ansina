from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from ansina.heart.curation import CurationEvent
from ansina.heart.eval.extraction_runner import (
    ExtractionReport,
    FailureFixtureResult,
    InterventionFixtureResult,
    TitleFixtureResult,
    run_extraction_bench,
)
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
    StructuredExtractor,
    TitleExtractionOutcome,
)
from ansina.opencode.interventions import OpencodeAction


def _candidate(run_id: str = "r1") -> InterventionCandidateRun:
    return InterventionCandidateRun(run_id=run_id, status="failed", detail="d")


def _intervention_fixture(
    fixture_id: str,
    *,
    expected_action: OpencodeAction | None,
    expected_run_id: str | None,
    tags: frozenset[str] = frozenset(),
) -> InterventionExtractionFixture:
    return InterventionExtractionFixture(
        id=fixture_id,
        candidates=(_candidate("r1"),),
        intake_paused=False,
        recent_failed=0,
        recent_timeout=0,
        recent_permission_denied=0,
        expected_action=expected_action,
        expected_run_id=expected_run_id,
        tags=tags,
    )


def _event(sequence: int = 0, *, payload: str = "p") -> CurationEvent:
    return CurationEvent(sequence=sequence, raw_type="text", payload=payload)


def _title_fixture(
    fixture_id: str, *, expected_title: str = "A title"
) -> TitleExtractionFixture:
    return TitleExtractionFixture(
        id=fixture_id,
        prompt="do it",
        events=(_event(0),),
        expected_title=expected_title,
    )


def _failure_fixture(
    fixture_id: str,
    *,
    expected_cause: FailureCause = FailureCause.SHELL_DENIED,
    expected_evidence_seq: int = 0,
) -> FailureExtractionFixture:
    return FailureExtractionFixture(
        id=fixture_id,
        events=(_event(0),),
        expected_cause=expected_cause,
        expected_evidence_seq=expected_evidence_seq,
    )


class _ScriptedExtractor:
    """A `StructuredExtractor` fake: one scripted outcome per job, consumed in call
    order, falling back to the last one once exhausted — mirroring
    `test_curation_runner.py`'s own `_ScriptedCurator` shape.
    """

    def __init__(
        self,
        *,
        interventions: Sequence[InterventionExtraction | None] = (),
        titles: Sequence[str | None] = (),
        failures: Sequence[FailureExtraction | None] = (),
    ) -> None:
        self._interventions = list(interventions)
        self._titles = list(titles)
        self._failures = list(failures)
        self.intervention_calls = 0
        self.title_calls = 0
        self.failure_calls = 0

    def extract_intervention(
        self,
        candidates: Sequence[InterventionCandidateRun],
        *,
        intake_paused: bool,
        recent_failed: int,
        recent_timeout: int,
        recent_permission_denied: int,
    ) -> InterventionExtractionOutcome:
        index = min(self.intervention_calls, len(self._interventions) - 1)
        self.intervention_calls += 1
        parsed = self._interventions[index]
        return InterventionExtractionOutcome(
            candidates=tuple(candidates),
            parsed=parsed,
            raw_output="scripted",
            prompt_tokens=10 + self.intervention_calls,
        )

    def extract_title(
        self, prompt: str, events: Sequence[CurationEvent]
    ) -> TitleExtractionOutcome:
        index = min(self.title_calls, len(self._titles) - 1)
        self.title_calls += 1
        parsed = self._titles[index]
        return TitleExtractionOutcome(
            prompt=prompt,
            parsed=parsed,
            raw_output="scripted",
            prompt_tokens=20 + self.title_calls,
        )

    def extract_failure_cause(
        self, events: Sequence[CurationEvent]
    ) -> FailureExtractionOutcome:
        index = min(self.failure_calls, len(self._failures) - 1)
        self.failure_calls += 1
        parsed = self._failures[index]
        return FailureExtractionOutcome(
            events=tuple(events),
            parsed=parsed,
            raw_output="scripted",
            prompt_tokens=30 + self.failure_calls,
        )


def _bench(
    extractor: StructuredExtractor,
    fixtures: Sequence[ExtractionFixture],
    *,
    chat_template: bool = False,
    commit: str | None = None,
    branch: str | None = None,
    perf_counter: Callable[[], float] = time.perf_counter,
) -> ExtractionReport:
    """Wraps `run_extraction_bench` with this test module's own fixed metadata
    defaults, so every test below only states what it actually varies.
    """
    return run_extraction_bench(
        extractor,
        fixtures,
        model_repo="m",
        prompt_variant="strict",
        max_output_tokens=50,
        chat_template=chat_template,
        commit=commit,
        branch=branch,
        perf_counter=perf_counter,
    )


# --- InterventionFixtureResult.direction / agrees -----------------------------------


def test_intervention_result_direction_is_none_without_schema_valid() -> None:
    result = InterventionFixtureResult(
        fixture_id="x",
        expected_action=OpencodeAction.RETRY,
        expected_run_id="r1",
        actual_action=None,
        actual_run_id=None,
        schema_valid=False,
        raw_output="???",
        prompt_tokens=1,
        tags=frozenset(),
    )
    assert result.direction is None
    assert result.agrees is False


def test_intervention_result_direction_agree() -> None:
    result = InterventionFixtureResult(
        fixture_id="x",
        expected_action=OpencodeAction.RETRY,
        expected_run_id="r1",
        actual_action=OpencodeAction.RETRY,
        actual_run_id="r1",
        schema_valid=True,
        raw_output="",
        prompt_tokens=1,
        tags=frozenset(),
    )
    assert result.direction == "agree"
    assert result.agrees is True


def test_intervention_result_direction_wrong_run() -> None:
    result = InterventionFixtureResult(
        fixture_id="x",
        expected_action=OpencodeAction.RETRY,
        expected_run_id="r1",
        actual_action=OpencodeAction.RETRY,
        actual_run_id="r2",
        schema_valid=True,
        raw_output="",
        prompt_tokens=1,
        tags=frozenset(),
    )
    assert result.direction == "wrong_run"
    assert result.agrees is False


def test_intervention_result_direction_more_conservative() -> None:
    result = InterventionFixtureResult(
        fixture_id="x",
        expected_action=OpencodeAction.RETRY,
        expected_run_id="r1",
        actual_action=OpencodeAction.PAUSE_INTAKE,
        actual_run_id=None,
        schema_valid=True,
        raw_output="",
        prompt_tokens=1,
        tags=frozenset(),
    )
    assert result.direction == "more_conservative"


def test_intervention_result_direction_less_conservative() -> None:
    result = InterventionFixtureResult(
        fixture_id="x",
        expected_action=OpencodeAction.PAUSE_INTAKE,
        expected_run_id=None,
        actual_action=OpencodeAction.RETRY,
        actual_run_id="r1",
        schema_valid=True,
        raw_output="",
        prompt_tokens=1,
        tags=frozenset(),
    )
    assert result.direction == "less_conservative"


def test_intervention_result_direction_agree_on_no_action() -> None:
    result = InterventionFixtureResult(
        fixture_id="x",
        expected_action=None,
        expected_run_id=None,
        actual_action=None,
        actual_run_id=None,
        schema_valid=True,
        raw_output="",
        prompt_tokens=1,
        tags=frozenset(),
    )
    assert result.direction == "agree"


# --- TitleFixtureResult / FailureFixtureResult --------------------------------------


def test_title_result_schema_valid() -> None:
    assert (
        TitleFixtureResult(
            fixture_id="x",
            expected_title="e",
            actual_title="a",
            raw_output="a",
            prompt_tokens=1,
            tags=frozenset(),
        ).schema_valid
        is True
    )
    assert (
        TitleFixtureResult(
            fixture_id="x",
            expected_title="e",
            actual_title=None,
            raw_output="???",
            prompt_tokens=1,
            tags=frozenset(),
        ).schema_valid
        is False
    )


def test_failure_result_schema_valid_and_correctness() -> None:
    correct = FailureFixtureResult(
        fixture_id="x",
        expected_cause=FailureCause.TIMEOUT,
        expected_evidence_seq=3,
        actual_cause=FailureCause.TIMEOUT,
        actual_evidence_seq=3,
        raw_output="",
        prompt_tokens=1,
        tags=frozenset(),
    )
    wrong_cause = FailureFixtureResult(
        fixture_id="x",
        expected_cause=FailureCause.TIMEOUT,
        expected_evidence_seq=3,
        actual_cause=FailureCause.OTHER,
        actual_evidence_seq=3,
        raw_output="",
        prompt_tokens=1,
        tags=frozenset(),
    )
    fallback = FailureFixtureResult(
        fixture_id="x",
        expected_cause=FailureCause.TIMEOUT,
        expected_evidence_seq=3,
        actual_cause=None,
        actual_evidence_seq=None,
        raw_output="???",
        prompt_tokens=1,
        tags=frozenset(),
    )
    assert correct.schema_valid is True
    assert correct.cause_correct is True
    assert correct.evidence_correct is True
    assert wrong_cause.cause_correct is False
    assert wrong_cause.evidence_correct is True
    assert fallback.schema_valid is False
    assert fallback.cause_correct is False
    assert fallback.evidence_correct is False


# --- run_extraction_bench ------------------------------------------------------------


def test_run_extraction_bench_dispatches_each_fixture_by_type() -> None:
    fixtures: list[ExtractionFixture] = [
        _intervention_fixture(
            "i1", expected_action=OpencodeAction.RETRY, expected_run_id="r1"
        ),
        _title_fixture("t1"),
        _failure_fixture("f1"),
    ]
    extractor = _ScriptedExtractor(
        interventions=[
            InterventionExtraction(action=OpencodeAction.RETRY, run_id="r1", reason="")
        ],
        titles=["A title"],
        failures=[
            FailureExtraction(cause=FailureCause.SHELL_DENIED, evidence_event_seq=0)
        ],
    )

    report = _bench(extractor, fixtures)

    assert [r.fixture_id for r in report.intervention_results] == ["i1"]
    assert [r.fixture_id for r in report.title_results] == ["t1"]
    assert [r.fixture_id for r in report.failure_results] == ["f1"]
    assert report.model_repo == "m"
    assert report.prompt_variant == "strict"
    assert report.max_output_tokens == 50
    assert report.chat_template is False
    assert report.commit is None
    assert report.branch is None


def test_run_extraction_bench_passes_through_commit_branch_and_chat_template() -> None:
    extractor = _ScriptedExtractor(titles=["A title"])
    report = _bench(
        extractor,
        [_title_fixture("t1")],
        commit="abc123",
        branch="main",
        chat_template=True,
    )
    assert report.commit == "abc123"
    assert report.branch == "main"
    assert report.chat_template is True


def test_run_extraction_bench_handles_an_empty_fixture_list() -> None:
    report = _bench(_ScriptedExtractor(), [])

    assert report.intervention_results == ()
    assert report.title_results == ()
    assert report.failure_results == ()
    assert report.intervention_schema_valid_rate == 0.0
    assert report.intervention_agreement_rate == 0.0
    assert report.intervention_parse_fallback_rate == 0.0
    assert report.title_schema_valid_rate == 0.0
    assert report.failure_schema_valid_rate == 0.0
    assert report.failure_cause_accuracy == 0.0
    assert report.latency_p50_seconds == 0.0
    assert report.latency_p95_seconds == 0.0
    assert report.prompt_tokens_min == 0
    assert report.prompt_tokens_median == 0.0
    assert report.prompt_tokens_max == 0


def test_run_extraction_bench_computes_intervention_metrics() -> None:
    fixtures = [
        _intervention_fixture(
            "agree", expected_action=OpencodeAction.RETRY, expected_run_id="r1"
        ),
        _intervention_fixture(
            "wrong_run", expected_action=OpencodeAction.RETRY, expected_run_id="r1"
        ),
        _intervention_fixture(
            "more_cons", expected_action=OpencodeAction.RETRY, expected_run_id="r1"
        ),
        _intervention_fixture(
            "less_cons",
            expected_action=OpencodeAction.PAUSE_INTAKE,
            expected_run_id=None,
        ),
        _intervention_fixture(
            "fallback", expected_action=OpencodeAction.RETRY, expected_run_id="r1"
        ),
    ]
    extractor = _ScriptedExtractor(
        interventions=[
            InterventionExtraction(action=OpencodeAction.RETRY, run_id="r1", reason=""),
            InterventionExtraction(action=OpencodeAction.RETRY, run_id="r1", reason=""),
            InterventionExtraction(
                action=OpencodeAction.PAUSE_INTAKE, run_id=None, reason=""
            ),
            InterventionExtraction(action=OpencodeAction.RETRY, run_id="r1", reason=""),
            None,
        ]
    )
    # Second fixture's own candidates don't matter for scripted output, only the
    # expected_run_id differs to force a wrong_run classification.
    fixtures[1] = _intervention_fixture(
        "wrong_run", expected_action=OpencodeAction.RETRY, expected_run_id="other"
    )

    report = _bench(extractor, fixtures)

    assert report.intervention_schema_valid_rate == 4 / 5
    assert report.intervention_hallucination_count == 0
    assert report.intervention_agreement_rate == 1 / 5
    assert report.intervention_more_conservative_count == 1
    assert report.intervention_less_conservative_count == 1
    assert report.intervention_wrong_run_count == 1
    assert report.intervention_parse_fallback_rate == 1 / 5


def test_run_extraction_bench_computes_title_metrics() -> None:
    fixtures = [_title_fixture("t1"), _title_fixture("t2")]
    extractor = _ScriptedExtractor(titles=["A title", None])

    report = _bench(extractor, fixtures)

    assert report.title_schema_valid_rate == 1 / 2
    assert report.title_parse_fallback_rate == 1 / 2


def test_run_extraction_bench_computes_failure_metrics() -> None:
    fixtures = [
        _failure_fixture(
            "correct", expected_cause=FailureCause.SHELL_DENIED, expected_evidence_seq=0
        ),
        _failure_fixture(
            "wrong_cause",
            expected_cause=FailureCause.SHELL_DENIED,
            expected_evidence_seq=0,
        ),
        _failure_fixture(
            "fallback",
            expected_cause=FailureCause.SHELL_DENIED,
            expected_evidence_seq=0,
        ),
    ]
    extractor = _ScriptedExtractor(
        failures=[
            FailureExtraction(cause=FailureCause.SHELL_DENIED, evidence_event_seq=0),
            FailureExtraction(cause=FailureCause.OTHER, evidence_event_seq=0),
            None,
        ]
    )

    report = _bench(extractor, fixtures)

    assert report.failure_schema_valid_rate == 2 / 3
    assert report.failure_cause_accuracy == 1 / 3
    assert report.failure_evidence_accuracy == 2 / 3
    assert report.failure_parse_fallback_rate == 1 / 3


def test_run_extraction_bench_latency_and_prompt_token_stats() -> None:
    perf_values = iter([0.0, 0.1, 0.2, 0.4])

    def fake_perf_counter() -> float:
        return next(perf_values)

    extractor = _ScriptedExtractor(titles=["A title", "Another title"])
    report = _bench(
        extractor,
        [_title_fixture("t1"), _title_fixture("t2")],
        perf_counter=fake_perf_counter,
    )

    assert report.latency_p50_seconds > 0.0
    assert report.latency_p95_seconds > 0.0
    assert report.prompt_tokens_min == 21
    assert report.prompt_tokens_max == 22
    assert report.prompt_tokens_median == 21.5


def test_run_extraction_bench_returns_an_extraction_report() -> None:
    report = _bench(_ScriptedExtractor(titles=["A title"]), [_title_fixture("t1")])
    assert isinstance(report, ExtractionReport)
