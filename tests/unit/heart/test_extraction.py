from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from ansina.heart.curation import CurationEvent
from ansina.heart.extraction import (
    DEFAULT_EXTRACTION_TEMPLATES,
    DEFAULT_EXTRACTION_VARIANT,
    EXTRACTION_PROMPT_VARIANTS,
    ExtractionJob,
    FailureCause,
    FailureExtraction,
    FailureExtractionOutcome,
    HeartStructuredExtractor,
    InterventionCandidateRun,
    InterventionExtraction,
    InterventionExtractionOutcome,
    StructuredExtractor,
    TitleExtractionOutcome,
    try_parse_failure,
    try_parse_intervention,
    try_parse_title,
)
from ansina.heart.runtime import BaseHeartRuntime
from ansina.opencode.interventions import OpencodeAction


class _FakeExtractionHeart(BaseHeartRuntime):
    """Mirrors `test_triage.py`'s/`test_curation.py`'s own fake heart: 1 token per
    character, already loaded, with a scripted reply per call (falling back to the
    last one once exhausted) or a fixed reply for every call.
    """

    def __init__(
        self,
        *,
        context_tokens: int = 1000,
        max_output_tokens: int = 50,
        reply: str | None = "{}",
        replies: list[str] | None = None,
    ) -> None:
        super().__init__(
            context_tokens=context_tokens, max_output_tokens=max_output_tokens
        )
        self.load()
        self._reply = reply
        self._replies = replies
        self.generate_calls = 0
        self.last_prompt: str | None = None
        self.last_max_tokens: int | None = None

    def _load_backend(self) -> None:
        pass

    def _generate(self, prompt: str, max_tokens: int) -> str:
        self.last_prompt = prompt
        self.last_max_tokens = max_tokens
        self.generate_calls += 1
        if self._replies is not None:
            index = min(self.generate_calls - 1, len(self._replies) - 1)
            return self._replies[index]
        assert self._reply is not None
        return self._reply

    def _token_count(self, text: str) -> int:
        return len(text)

    def _unload_backend(self) -> None:
        pass


def _candidate(
    run_id: str = "r1", *, status: str = "failed", detail: str = "d"
) -> InterventionCandidateRun:
    return InterventionCandidateRun(run_id=run_id, status=status, detail=detail)


def _event(
    sequence: int, *, raw_type: str = "text", payload: str = "p"
) -> CurationEvent:
    return CurationEvent(sequence=sequence, raw_type=raw_type, payload=payload)


# --- FailureCause / ExtractionJob --------------------------------------------------


def test_failure_cause_values_are_stable() -> None:
    assert FailureCause.SHELL_DENIED.value == "shell_denied"
    assert FailureCause.PERMISSION_DENIED.value == "permission_denied"
    assert FailureCause.TIMEOUT.value == "timeout"
    assert FailureCause.ERROR.value == "error"
    assert FailureCause.OTHER.value == "other"


def test_extraction_job_values_are_stable() -> None:
    assert ExtractionJob.INTERVENTION.value == "intervention"
    assert ExtractionJob.TITLE.value == "title"
    assert ExtractionJob.FAILURE_CAUSE.value == "failure_cause"


# --- try_parse_intervention --------------------------------------------------------


def test_try_parse_intervention_parses_a_valid_reply() -> None:
    raw = '{"action": "retry", "run_id": "r1", "reason": "it failed"}'
    result = try_parse_intervention(raw, valid_run_ids=frozenset({"r1"}))
    assert result == InterventionExtraction(
        action=OpencodeAction.RETRY, run_id="r1", reason="it failed"
    )


def test_try_parse_intervention_recognizes_the_literal_none_action() -> None:
    raw = '{"action": "none", "run_id": null, "reason": "nothing wrong"}'
    result = try_parse_intervention(raw, valid_run_ids=frozenset({"r1"}))
    assert result == InterventionExtraction(
        action=None, run_id=None, reason="nothing wrong"
    )


def test_try_parse_intervention_is_case_insensitive_for_the_none_action() -> None:
    raw = '{"action": "None", "run_id": null, "reason": ""}'
    result = try_parse_intervention(raw, valid_run_ids=frozenset())
    assert result is not None
    assert result.action is None


def test_try_parse_intervention_defaults_a_missing_reason_to_empty() -> None:
    raw = '{"action": "none", "run_id": null}'
    result = try_parse_intervention(raw, valid_run_ids=frozenset())
    assert result is not None
    assert result.reason == ""


def test_try_parse_intervention_truncates_an_over_long_reason() -> None:
    reason = "x" * 500
    raw = f'{{"action": "none", "run_id": null, "reason": "{reason}"}}'
    result = try_parse_intervention(raw, valid_run_ids=frozenset())
    assert result is not None
    assert len(result.reason) == 200


def test_try_parse_intervention_ignores_a_non_string_reason() -> None:
    raw = '{"action": "none", "run_id": null, "reason": 7}'
    result = try_parse_intervention(raw, valid_run_ids=frozenset())
    assert result is not None
    assert result.reason == ""


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        '{"run_id": "r1"}',
        '{"action": 7}',
        '{"action": "bogus_action"}',
    ],
)
def test_try_parse_intervention_returns_none_on_malformed_or_out_of_vocabulary(
    raw: str,
) -> None:
    assert try_parse_intervention(raw, valid_run_ids=frozenset({"r1"})) is None


def test_try_parse_intervention_refuses_a_hallucinated_run_id() -> None:
    raw = '{"action": "retry", "run_id": "ghost", "reason": "x"}'
    assert try_parse_intervention(raw, valid_run_ids=frozenset({"r1"})) is None


def test_try_parse_intervention_refuses_a_non_string_run_id() -> None:
    raw = '{"action": "retry", "run_id": 7, "reason": "x"}'
    assert try_parse_intervention(raw, valid_run_ids=frozenset({"r1"})) is None


def test_try_parse_intervention_returns_none_on_an_unclosed_brace() -> None:
    """Exercises `_first_balanced_object`'s own "never closes" branch — a `{` with
    no matching `}` anywhere in the rest of the text.
    """
    raw = "preamble { action: retry, no closing brace here"
    assert try_parse_intervention(raw, valid_run_ids=frozenset({"r1"})) is None


def test_try_parse_intervention_extracts_json_from_a_code_fence() -> None:
    raw = (
        'Sure, here it is:\n```json\n{"action": "nudge", "run_id": "r1", '
        '"reason": "stalled"}\n```'
    )
    result = try_parse_intervention(raw, valid_run_ids=frozenset({"r1"}))
    assert result == InterventionExtraction(
        action=OpencodeAction.NUDGE, run_id="r1", reason="stalled"
    )


def test_try_parse_intervention_uses_text_after_the_last_think_close_tag() -> None:
    raw = (
        "<think>maybe retry?</think>"
        '{"action": "pause_intake", "run_id": null, "reason": "too many failures"}'
    )
    result = try_parse_intervention(raw, valid_run_ids=frozenset())
    assert result is not None
    assert result.action is OpencodeAction.PAUSE_INTAKE


def test_try_parse_intervention_never_logs(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    try_parse_intervention("nonsense", valid_run_ids=frozenset())
    assert captured_logs() == []


# --- try_parse_title ----------------------------------------------------------------


def test_try_parse_title_returns_the_stripped_single_line() -> None:
    assert try_parse_title("  Fix the off-by-one bug  ") == "Fix the off-by-one bug"


@pytest.mark.parametrize("raw", ["", "   ", "line one\nline two"])
def test_try_parse_title_returns_none_when_empty_or_multiline(raw: str) -> None:
    assert try_parse_title(raw) is None


def test_try_parse_title_returns_none_when_over_length() -> None:
    raw = "x" * 81
    assert try_parse_title(raw, max_length=80) is None


def test_try_parse_title_accepts_exactly_the_max_length() -> None:
    raw = "x" * 80
    assert try_parse_title(raw, max_length=80) == raw


def test_try_parse_title_uses_text_after_the_last_think_close_tag() -> None:
    raw = "<think>hmm</think>A short title"
    assert try_parse_title(raw) == "A short title"


def test_try_parse_title_never_logs(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    try_parse_title("")
    assert captured_logs() == []


# --- try_parse_failure ---------------------------------------------------------------


def test_try_parse_failure_parses_a_valid_reply() -> None:
    raw = '{"cause": "shell_denied", "evidence_event_seq": 3}'
    result = try_parse_failure(raw, valid_sequences=frozenset({1, 3, 5}))
    assert result == FailureExtraction(
        cause=FailureCause.SHELL_DENIED, evidence_event_seq=3
    )


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '{"evidence_event_seq": 3}',
        '{"cause": "bogus"}',
        '{"cause": 7, "evidence_event_seq": 3}',
    ],
)
def test_try_parse_failure_returns_none_on_malformed_or_out_of_vocabulary(
    raw: str,
) -> None:
    assert try_parse_failure(raw, valid_sequences=frozenset({3})) is None


def test_try_parse_failure_refuses_a_hallucinated_sequence() -> None:
    raw = '{"cause": "timeout", "evidence_event_seq": 99}'
    assert try_parse_failure(raw, valid_sequences=frozenset({1, 2})) is None


def test_try_parse_failure_refuses_a_boolean_sequence() -> None:
    raw = '{"cause": "timeout", "evidence_event_seq": true}'
    assert try_parse_failure(raw, valid_sequences=frozenset({1})) is None


def test_try_parse_failure_extracts_json_from_a_preamble() -> None:
    raw = 'The cause is: {"cause": "error", "evidence_event_seq": 2} as shown.'
    result = try_parse_failure(raw, valid_sequences=frozenset({2}))
    assert result == FailureExtraction(cause=FailureCause.ERROR, evidence_event_seq=2)


def test_try_parse_failure_uses_text_after_the_last_think_close_tag() -> None:
    raw = '<think>hmm</think>{"cause": "other", "evidence_event_seq": 0}'
    result = try_parse_failure(raw, valid_sequences=frozenset({0}))
    assert result is not None
    assert result.cause is FailureCause.OTHER


def test_try_parse_failure_never_logs(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    try_parse_failure("nonsense", valid_sequences=frozenset())
    assert captured_logs() == []


# --- Outcome dataclasses -------------------------------------------------------------


def test_intervention_extraction_outcome_parse_fallback() -> None:
    parsed = InterventionExtractionOutcome(
        candidates=(),
        parsed=InterventionExtraction(action=None, run_id=None, reason=""),
        raw_output="{}",
        prompt_tokens=1,
    )
    fallback = InterventionExtractionOutcome(
        candidates=(), parsed=None, raw_output="???", prompt_tokens=1
    )
    assert parsed.parse_fallback is False
    assert fallback.parse_fallback is True


def test_title_extraction_outcome_parse_fallback() -> None:
    parsed = TitleExtractionOutcome(
        prompt="p", parsed="A title", raw_output="A title", prompt_tokens=1
    )
    fallback = TitleExtractionOutcome(
        prompt="p", parsed=None, raw_output="", prompt_tokens=1
    )
    assert parsed.parse_fallback is False
    assert fallback.parse_fallback is True


def test_failure_extraction_outcome_parse_fallback() -> None:
    parsed = FailureExtractionOutcome(
        events=(),
        parsed=FailureExtraction(cause=FailureCause.OTHER, evidence_event_seq=0),
        raw_output="{}",
        prompt_tokens=1,
    )
    fallback = FailureExtractionOutcome(
        events=(), parsed=None, raw_output="???", prompt_tokens=1
    )
    assert parsed.parse_fallback is False
    assert fallback.parse_fallback is True


# --- EXTRACTION_PROMPT_VARIANTS -------------------------------------------------------


def test_default_extraction_variant_is_strict() -> None:
    assert DEFAULT_EXTRACTION_VARIANT == "strict"
    assert DEFAULT_EXTRACTION_TEMPLATES is EXTRACTION_PROMPT_VARIANTS["strict"]


def test_every_extraction_variant_carries_all_three_jobs() -> None:
    for name, templates in EXTRACTION_PROMPT_VARIANTS.items():
        assert set(templates) == set(ExtractionJob), f"{name} is missing a job"


def test_every_intervention_template_has_a_state_placeholder() -> None:
    for name, templates in EXTRACTION_PROMPT_VARIANTS.items():
        rendered = templates[ExtractionJob.INTERVENTION].format(state="STATEBLOCK")
        assert "{state}" not in rendered, f"{name} left an unfilled placeholder"
        assert "STATEBLOCK" in rendered, f"{name} never substituted state"


def test_every_title_template_has_prompt_and_events_placeholders() -> None:
    for name, templates in EXTRACTION_PROMPT_VARIANTS.items():
        rendered = templates[ExtractionJob.TITLE].format(
            prompt="PROMPTTEXT", events="EVENTLIST"
        )
        assert "{prompt}" not in rendered, f"{name} left an unfilled prompt"
        assert "{events}" not in rendered, f"{name} left an unfilled events"
        assert "PROMPTTEXT" in rendered
        assert "EVENTLIST" in rendered


def test_every_failure_template_has_an_events_placeholder() -> None:
    for name, templates in EXTRACTION_PROMPT_VARIANTS.items():
        rendered = templates[ExtractionJob.FAILURE_CAUSE].format(events="EVENTLIST")
        assert "{events}" not in rendered, f"{name} left an unfilled placeholder"
        assert "EVENTLIST" in rendered


def test_extraction_variants_are_distinct_per_job() -> None:
    for job in ExtractionJob:
        templates = [variant[job] for variant in EXTRACTION_PROMPT_VARIANTS.values()]
        assert len(templates) == len(set(templates))


# --- HeartStructuredExtractor ---------------------------------------------------------


def test_heart_structured_extractor_satisfies_the_protocol() -> None:
    heart = _FakeExtractionHeart()
    extractor: StructuredExtractor = HeartStructuredExtractor(heart)
    assert isinstance(extractor, StructuredExtractor)


def test_extract_intervention_returns_the_parsed_result() -> None:
    heart = _FakeExtractionHeart(
        reply='{"action": "retry", "run_id": "r1", "reason": "failed once"}'
    )
    extractor = HeartStructuredExtractor(heart)

    outcome = extractor.extract_intervention(
        [_candidate("r1")],
        intake_paused=False,
        recent_failed=1,
        recent_timeout=0,
        recent_permission_denied=0,
    )

    assert outcome.parsed == InterventionExtraction(
        action=OpencodeAction.RETRY, run_id="r1", reason="failed once"
    )
    assert outcome.parse_fallback is False
    assert outcome.candidates == (_candidate("r1"),)
    assert heart.last_prompt is not None
    assert "[r1] failed: d" in heart.last_prompt
    assert "Intake: accepting new runs." in heart.last_prompt
    assert "1 failed, 0 timed out, 0 permission denial(s)" in heart.last_prompt


def test_extract_intervention_reports_a_paused_intake() -> None:
    heart = _FakeExtractionHeart(reply='{"action": "none", "run_id": null}')
    extractor = HeartStructuredExtractor(heart)

    extractor.extract_intervention(
        [_candidate("r1")],
        intake_paused=True,
        recent_failed=0,
        recent_timeout=0,
        recent_permission_denied=0,
    )

    assert heart.last_prompt is not None
    assert "Intake: paused." in heart.last_prompt


def test_extract_intervention_reports_no_active_or_recent_runs_when_empty() -> None:
    heart = _FakeExtractionHeart(reply='{"action": "none", "run_id": null}')
    extractor = HeartStructuredExtractor(heart)

    extractor.extract_intervention(
        [],
        intake_paused=False,
        recent_failed=0,
        recent_timeout=0,
        recent_permission_denied=0,
    )

    assert heart.last_prompt is not None
    assert "(no active or recent OpenCode runs)" in heart.last_prompt


def test_extract_intervention_logs_a_warning_on_parse_fallback(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    heart = _FakeExtractionHeart(reply="not json")
    extractor = HeartStructuredExtractor(heart)

    outcome = extractor.extract_intervention(
        [_candidate("r1")],
        intake_paused=False,
        recent_failed=0,
        recent_timeout=0,
        recent_permission_denied=0,
    )

    assert outcome.parsed is None
    assert outcome.parse_fallback is True
    assert any(line["level"] == "WARNING" for line in captured_logs())


def test_extract_intervention_prompt_tokens_matches_the_rendered_prompt() -> None:
    heart = _FakeExtractionHeart(reply='{"action": "none", "run_id": null}')
    extractor = HeartStructuredExtractor(heart)

    outcome = extractor.extract_intervention(
        [_candidate("r1")],
        intake_paused=False,
        recent_failed=0,
        recent_timeout=0,
        recent_permission_denied=0,
    )

    assert heart.last_prompt is not None
    assert outcome.prompt_tokens == heart.token_count(heart.last_prompt)


def test_extract_title_returns_the_parsed_title() -> None:
    heart = _FakeExtractionHeart(reply="Fix the off-by-one bug")
    extractor = HeartStructuredExtractor(heart)

    outcome = extractor.extract_title("please fix the bug", [_event(0, payload="hi")])

    assert outcome.parsed == "Fix the off-by-one bug"
    assert outcome.parse_fallback is False
    assert outcome.prompt == "please fix the bug"
    assert heart.last_prompt is not None
    assert "please fix the bug" in heart.last_prompt
    assert "[0] text: hi" in heart.last_prompt


def test_extract_title_logs_a_warning_on_parse_fallback(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    heart = _FakeExtractionHeart(reply="line one\nline two")
    extractor = HeartStructuredExtractor(heart)

    outcome = extractor.extract_title("do it", [_event(0)])

    assert outcome.parsed is None
    assert outcome.parse_fallback is True
    assert any(line["level"] == "WARNING" for line in captured_logs())


def test_extract_title_respects_the_configured_max_title_length() -> None:
    heart = _FakeExtractionHeart(reply="x" * 10)
    extractor = HeartStructuredExtractor(heart, max_title_length=5)

    outcome = extractor.extract_title("do it", [_event(0)])

    assert outcome.parsed is None


def test_extract_failure_cause_returns_the_parsed_result() -> None:
    heart = _FakeExtractionHeart(
        reply='{"cause": "shell_denied", "evidence_event_seq": 0}'
    )
    extractor = HeartStructuredExtractor(heart)

    outcome = extractor.extract_failure_cause([_event(0, payload="denied")])

    assert outcome.parsed == FailureExtraction(
        cause=FailureCause.SHELL_DENIED, evidence_event_seq=0
    )
    assert outcome.parse_fallback is False
    assert outcome.events == (_event(0, payload="denied"),)
    assert heart.last_prompt is not None
    assert "[0] text: denied" in heart.last_prompt


def test_extract_failure_cause_logs_a_warning_on_parse_fallback(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    heart = _FakeExtractionHeart(reply="not json")
    extractor = HeartStructuredExtractor(heart)

    outcome = extractor.extract_failure_cause([_event(0)])

    assert outcome.parsed is None
    assert outcome.parse_fallback is True
    assert any(line["level"] == "WARNING" for line in captured_logs())


def test_extract_failure_cause_refuses_a_sequence_outside_its_own_events() -> None:
    heart = _FakeExtractionHeart(reply='{"cause": "timeout", "evidence_event_seq": 99}')
    extractor = HeartStructuredExtractor(heart)

    outcome = extractor.extract_failure_cause([_event(0), _event(1)])

    assert outcome.parsed is None


def test_heart_structured_extractor_passes_max_output_tokens_through() -> None:
    heart = _FakeExtractionHeart(
        reply='{"action": "none", "run_id": null}', max_output_tokens=999
    )
    extractor = HeartStructuredExtractor(heart, max_output_tokens=7)

    extractor.extract_intervention(
        [_candidate("r1")],
        intake_paused=False,
        recent_failed=0,
        recent_timeout=0,
        recent_permission_denied=0,
    )

    assert heart.last_max_tokens == 7


def test_heart_structured_extractor_defaults_max_output_tokens_to_runtime_default() -> (
    None
):
    heart = _FakeExtractionHeart(
        reply='{"action": "none", "run_id": null}', max_output_tokens=42
    )
    extractor = HeartStructuredExtractor(heart)

    extractor.extract_intervention(
        [_candidate("r1")],
        intake_paused=False,
        recent_failed=0,
        recent_timeout=0,
        recent_permission_denied=0,
    )

    assert heart.last_max_tokens == 42
