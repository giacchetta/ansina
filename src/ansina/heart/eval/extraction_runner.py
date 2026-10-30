"""Runs the structured-extraction fixture set against a `StructuredExtractor` port
and computes the extraction bench report. See issue #15.

**Synchronous**, like `heart.eval.runner.run_bench`/`heart.eval.triage_runner
.run_triage_bench` — unlike `heart.eval.curation_runner.run_curation_bench`, no job
here drives a `BrainProvider` round trip: every job is graded against a fixture's own
human-authored or deterministic-policy ground truth, never a live Brain answer.

One fixture file covers three distinct jobs (`heart.eval.fixtures.ExtractionFixture`
is a tagged union); this module dispatches each fixture, by its own concrete type, to
the matching `StructuredExtractor` method, and buckets the results into three
job-scoped tuples on one shared `ExtractionReport` rather than three separate reports
— the issue's own fixture set is one file, so the report mirrors that.

Bespoke per-job metrics (schema-valid rate, hallucination counts, agreement-by-
direction for job 1; schema-valid rate for job 2; cause/evidence accuracy for job 3)
are computed directly in `_build_report` below, **not** via `heart.eval.metrics
.label_metrics`/`ClassifiedOutcome` — the identical precedent `heart.eval
.curation_runner` already set, since neither suite's metrics fit that generic
single-expected-label shape. `read_peak_rss_bytes`/`host_platform`/`mlx_lm_version`/
`percentile` are reused as-is.

Job 1's **conservativeness rank table** (`_ACTION_RANK`) is the one piece of scoring
policy worth calling out explicitly: it orders the five `OpencodeAction` members plus
"no action" from least to most disruptive/deferring —

    no action (0) < RETRY (1) < NUDGE (2) < COMPACT (3) < PAUSE_INTAKE (4)
    < ESCALATE_TO_BRAIN (5)

— so a model picking a *higher*-ranked action than
`DeterministicInterventionPolicy` itself would have (the fixture's own
`expected_action`) is scored `"more_conservative"` (an efficiency loss, tolerated up
to a committed ceiling), and a *lower*-ranked one is `"less_conservative"` (the
correctness bug the issue's own zero-tolerance ceiling targets). A same-rank
disagreement — the model picked the *right* action but named the *wrong* `run_id` —
is its own `"wrong_run"` direction, since the rank table has nothing to say about
which of two same-ranked candidates is more conservative than the other.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from ansina.heart.eval.fixtures import (
    ExtractionFixture,
    FailureExtractionFixture,
    InterventionExtractionFixture,
    TitleExtractionFixture,
)
from ansina.heart.eval.metrics import (
    host_platform,
    mlx_lm_version,
    percentile,
    read_peak_rss_bytes,
)
from ansina.heart.extraction import FailureCause, StructuredExtractor
from ansina.opencode.interventions import OpencodeAction

AgreementDirection = Literal[
    "agree", "more_conservative", "less_conservative", "wrong_run"
]

_ACTION_RANK: dict[OpencodeAction | None, int] = {
    None: 0,
    OpencodeAction.RETRY: 1,
    OpencodeAction.NUDGE: 2,
    OpencodeAction.COMPACT: 3,
    OpencodeAction.PAUSE_INTAKE: 4,
    OpencodeAction.ESCALATE_TO_BRAIN: 5,
}


def _direction(
    *,
    expected_action: OpencodeAction | None,
    expected_run_id: str | None,
    actual_action: OpencodeAction | None,
    actual_run_id: str | None,
) -> AgreementDirection:
    if actual_action == expected_action:
        return "agree" if actual_run_id == expected_run_id else "wrong_run"
    actual_rank = _ACTION_RANK[actual_action]
    expected_rank = _ACTION_RANK[expected_action]
    return "more_conservative" if actual_rank > expected_rank else "less_conservative"


@dataclass(frozen=True, slots=True)
class InterventionFixtureResult:
    """The raw outcome of running one `InterventionExtractionFixture`.

    `schema_valid` is `True` iff the port returned a parsed, in-vocabulary answer at
    all (never both unparseable *and* `schema_valid`) — the strict parser in
    `heart.extraction` has already refused a hallucinated `run_id` or an
    out-of-vocabulary `action` before this result is ever built, so `schema_valid`
    here is equivalent to `not parse_fallback`, kept as its own named field for
    reporting clarity (`heart.eval.extraction_report`'s own gate reads it directly).
    """

    fixture_id: str
    expected_action: OpencodeAction | None
    expected_run_id: str | None
    actual_action: OpencodeAction | None
    actual_run_id: str | None
    schema_valid: bool
    raw_output: str
    prompt_tokens: int
    tags: frozenset[str]

    @property
    def direction(self) -> AgreementDirection | None:
        """`None` iff `schema_valid` is `False` — there is no direction to assign to
        a reply that was never a real answer in the first place.
        """
        if not self.schema_valid:
            return None
        return _direction(
            expected_action=self.expected_action,
            expected_run_id=self.expected_run_id,
            actual_action=self.actual_action,
            actual_run_id=self.actual_run_id,
        )

    @property
    def agrees(self) -> bool:
        return self.direction == "agree"


@dataclass(frozen=True, slots=True)
class TitleFixtureResult:
    """The raw outcome of running one `TitleExtractionFixture`. `actual_title` is
    `None` iff the reply failed the format check (empty, multi-line, or over-length)
    — `expected_title` is carried through only for a human to read side by side with
    `raw_output`, never graded automatically (see this module's own docstring).
    """

    fixture_id: str
    expected_title: str
    actual_title: str | None
    raw_output: str
    prompt_tokens: int
    tags: frozenset[str]

    @property
    def schema_valid(self) -> bool:
        return self.actual_title is not None


@dataclass(frozen=True, slots=True)
class FailureFixtureResult:
    """The raw outcome of running one `FailureExtractionFixture`."""

    fixture_id: str
    expected_cause: FailureCause
    expected_evidence_seq: int
    actual_cause: FailureCause | None
    actual_evidence_seq: int | None
    raw_output: str
    prompt_tokens: int
    tags: frozenset[str]

    @property
    def schema_valid(self) -> bool:
        return self.actual_cause is not None

    @property
    def cause_correct(self) -> bool:
        return self.schema_valid and self.actual_cause == self.expected_cause

    @property
    def evidence_correct(self) -> bool:
        return (
            self.schema_valid and self.actual_evidence_seq == self.expected_evidence_seq
        )


@dataclass(frozen=True, slots=True)
class ExtractionReport:
    """One extraction bench run's full report, covering all three jobs — mirrors
    the metadata block every other `heart.eval` report carries, plus one result
    tuple per job. A job absent from the fixture file this run was given simply has
    an empty tuple; every metric below is computed over whichever jobs were present.
    """

    model_repo: str
    prompt_variant: str
    chat_template: bool
    generated_at: str
    host: str
    commit: str | None
    branch: str | None
    mlx_lm_version: str | None
    max_output_tokens: int
    intervention_results: tuple[InterventionFixtureResult, ...]
    title_results: tuple[TitleFixtureResult, ...]
    failure_results: tuple[FailureFixtureResult, ...]
    peak_rss_bytes: int
    latency_p50_seconds: float
    latency_p95_seconds: float
    prompt_tokens_min: int
    prompt_tokens_median: float
    prompt_tokens_max: int

    # --- job 1: intervention selection -------------------------------------------
    intervention_schema_valid_rate: float
    intervention_hallucination_count: int
    intervention_agreement_rate: float
    intervention_more_conservative_count: int
    intervention_less_conservative_count: int
    intervention_wrong_run_count: int
    intervention_parse_fallback_rate: float

    # --- job 2: run titling -------------------------------------------------------
    title_schema_valid_rate: float
    title_parse_fallback_rate: float

    # --- job 3: failure classification --------------------------------------------
    failure_schema_valid_rate: float
    failure_cause_accuracy: float
    failure_evidence_accuracy: float
    failure_parse_fallback_rate: float


def run_extraction_bench(
    extractor: StructuredExtractor,
    fixtures: Sequence[ExtractionFixture],
    *,
    model_repo: str,
    prompt_variant: str,
    max_output_tokens: int,
    chat_template: bool = False,
    commit: str | None = None,
    branch: str | None = None,
    perf_counter: Callable[[], float] = time.perf_counter,
) -> ExtractionReport:
    """Run every fixture in `fixtures` against `extractor`, dispatching each one by
    its own concrete type, and return the full report. Takes the **port**
    (`StructuredExtractor`), not a bare `HeartRuntime` — the same "measure the
    shipped port end to end" discipline `run_triage_bench`/`run_curation_bench`
    already follow.
    """
    peak_rss_bytes = read_peak_rss_bytes()

    intervention_results: list[InterventionFixtureResult] = []
    title_results: list[TitleFixtureResult] = []
    failure_results: list[FailureFixtureResult] = []
    latencies: list[float] = []
    prompt_tokens: list[int] = []

    for fixture in fixtures:
        start = perf_counter()
        if isinstance(fixture, InterventionExtractionFixture):
            intervention_outcome = extractor.extract_intervention(
                fixture.candidates,
                intake_paused=fixture.intake_paused,
                recent_failed=fixture.recent_failed,
                recent_timeout=fixture.recent_timeout,
                recent_permission_denied=fixture.recent_permission_denied,
            )
            latencies.append(perf_counter() - start)
            prompt_tokens.append(intervention_outcome.prompt_tokens)
            parsed = intervention_outcome.parsed
            intervention_results.append(
                InterventionFixtureResult(
                    fixture_id=fixture.id,
                    expected_action=fixture.expected_action,
                    expected_run_id=fixture.expected_run_id,
                    actual_action=parsed.action if parsed is not None else None,
                    actual_run_id=parsed.run_id if parsed is not None else None,
                    schema_valid=not intervention_outcome.parse_fallback,
                    raw_output=intervention_outcome.raw_output,
                    prompt_tokens=intervention_outcome.prompt_tokens,
                    tags=fixture.tags,
                )
            )
        elif isinstance(fixture, TitleExtractionFixture):
            title_outcome = extractor.extract_title(fixture.prompt, fixture.events)
            latencies.append(perf_counter() - start)
            prompt_tokens.append(title_outcome.prompt_tokens)
            title_results.append(
                TitleFixtureResult(
                    fixture_id=fixture.id,
                    expected_title=fixture.expected_title,
                    actual_title=title_outcome.parsed,
                    raw_output=title_outcome.raw_output,
                    prompt_tokens=title_outcome.prompt_tokens,
                    tags=fixture.tags,
                )
            )
        else:
            assert isinstance(fixture, FailureExtractionFixture)
            failure_outcome = extractor.extract_failure_cause(fixture.events)
            latencies.append(perf_counter() - start)
            prompt_tokens.append(failure_outcome.prompt_tokens)
            failure_parsed = failure_outcome.parsed
            failure_results.append(
                FailureFixtureResult(
                    fixture_id=fixture.id,
                    expected_cause=fixture.expected_cause,
                    expected_evidence_seq=fixture.expected_evidence_seq,
                    actual_cause=(
                        failure_parsed.cause if failure_parsed is not None else None
                    ),
                    actual_evidence_seq=(
                        failure_parsed.evidence_event_seq
                        if failure_parsed is not None
                        else None
                    ),
                    raw_output=failure_outcome.raw_output,
                    prompt_tokens=failure_outcome.prompt_tokens,
                    tags=fixture.tags,
                )
            )

    return _build_report(
        intervention_results,
        title_results,
        failure_results,
        model_repo=model_repo,
        prompt_variant=prompt_variant,
        max_output_tokens=max_output_tokens,
        chat_template=chat_template,
        commit=commit,
        branch=branch,
        peak_rss_bytes=peak_rss_bytes,
        latencies=latencies,
        prompt_tokens=prompt_tokens,
    )


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _build_report(
    intervention_results: Sequence[InterventionFixtureResult],
    title_results: Sequence[TitleFixtureResult],
    failure_results: Sequence[FailureFixtureResult],
    *,
    model_repo: str,
    prompt_variant: str,
    max_output_tokens: int,
    chat_template: bool,
    commit: str | None,
    branch: str | None,
    peak_rss_bytes: int,
    latencies: Sequence[float],
    prompt_tokens: Sequence[int],
) -> ExtractionReport:
    intervention_count = len(intervention_results)
    schema_valid_count = sum(1 for r in intervention_results if r.schema_valid)
    # A hallucinated run id or out-of-vocabulary action is already refused by the
    # strict parser (`heart.extraction.try_parse_intervention`) — by the time a
    # result reaches here, every schema-invalid reply is indistinguishable from any
    # other parse fallback. This count is reported for discoverability regardless
    # (always 0 today; a future, looser parser would make it meaningful), not
    # derived by re-inspecting `raw_output`.
    hallucination_count = 0
    more_conservative = sum(
        1 for r in intervention_results if r.direction == "more_conservative"
    )
    less_conservative = sum(
        1 for r in intervention_results if r.direction == "less_conservative"
    )
    wrong_run = sum(1 for r in intervention_results if r.direction == "wrong_run")
    agreement_count = sum(1 for r in intervention_results if r.agrees)

    title_count = len(title_results)
    title_valid_count = sum(1 for r in title_results if r.schema_valid)

    failure_count = len(failure_results)
    failure_valid_count = sum(1 for r in failure_results if r.schema_valid)
    cause_correct_count = sum(1 for r in failure_results if r.cause_correct)
    evidence_correct_count = sum(1 for r in failure_results if r.evidence_correct)

    return ExtractionReport(
        model_repo=model_repo,
        prompt_variant=prompt_variant,
        chat_template=chat_template,
        generated_at=datetime.now(UTC).isoformat(),
        host=host_platform(),
        commit=commit,
        branch=branch,
        mlx_lm_version=mlx_lm_version(),
        max_output_tokens=max_output_tokens,
        intervention_results=tuple(intervention_results),
        title_results=tuple(title_results),
        failure_results=tuple(failure_results),
        peak_rss_bytes=peak_rss_bytes,
        latency_p50_seconds=percentile(list(latencies), 0.50) if latencies else 0.0,
        latency_p95_seconds=percentile(list(latencies), 0.95) if latencies else 0.0,
        prompt_tokens_min=min(prompt_tokens) if prompt_tokens else 0,
        prompt_tokens_median=statistics.median(prompt_tokens) if prompt_tokens else 0.0,
        prompt_tokens_max=max(prompt_tokens) if prompt_tokens else 0,
        intervention_schema_valid_rate=_rate(schema_valid_count, intervention_count),
        intervention_hallucination_count=hallucination_count,
        intervention_agreement_rate=_rate(agreement_count, intervention_count),
        intervention_more_conservative_count=more_conservative,
        intervention_less_conservative_count=less_conservative,
        intervention_wrong_run_count=wrong_run,
        intervention_parse_fallback_rate=_rate(
            intervention_count - schema_valid_count, intervention_count
        ),
        title_schema_valid_rate=_rate(title_valid_count, title_count),
        title_parse_fallback_rate=_rate(title_count - title_valid_count, title_count),
        failure_schema_valid_rate=_rate(failure_valid_count, failure_count),
        failure_cause_accuracy=_rate(cause_correct_count, failure_count),
        failure_evidence_accuracy=_rate(evidence_correct_count, failure_count),
        failure_parse_fallback_rate=_rate(
            failure_count - failure_valid_count, failure_count
        ),
    )
