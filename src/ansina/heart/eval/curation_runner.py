"""Runs the context-curation fixture set against a `ContextCurator` port plus a real
`BrainProvider`, and computes the curation bench report. See issue #14.

The one bench suite in this harness that is genuinely **async**, unlike
`heart.eval.runner.run_bench`/`heart.eval.triage_runner.run_triage_bench` — it drives
real `BrainProvider.stream()` calls (issue #12's port), twice per fixture (curated
context vs. the recency-truncated baseline, at the identical token budget), to measure
the issue's own primary accept/reject metric: does the Brain's answer identify the
run's actual failure cause.

`_identifies_failure_cause` is a **documented, coarse proxy** for that judgment call,
not a semantic judge — a case-insensitive match of the fixture's `failure_cause`
label (verbatim, or every content word of it) against the Brain's free-text answer.
Decided with the project owner ahead of implementation: a human sanity-checks this
proxy against the raw answers (both kept on every `CurationFixtureResult`, unbounded
length like every other `raw_output`-shaped field in this harness) on the first real
run, the same "verify, don't assume" discipline this codebase applies to every other
heuristic it ships.
"""

from __future__ import annotations

import math
import re
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from ansina.brain.events import BrainErrorEvent, BrainTextDelta
from ansina.brain.provider import BrainMessage, BrainProvider, BrainRequest
from ansina.heart.curation import (
    ContextCurator,
    CurationEvent,
    recency_window_selection,
    render_event,
)
from ansina.heart.eval.fixtures import CurationFixture
from ansina.heart.eval.metrics import (
    host_platform,
    mlx_lm_version,
    percentile,
    read_peak_rss_bytes,
)

DEFAULT_BUDGET_FRACTION = 0.5

FAILURE_QUESTION_TEMPLATE = (
    "Here is a partial transcript of an OpenCode coding-agent run. What caused it "
    "to end the way it did? Answer in 1-3 sentences.\n\nTranscript:\n{transcript}"
)


@dataclass(frozen=True, slots=True)
class CurationFixtureResult:
    """The raw outcome of running one curation fixture through both conditions
    (curated, recency-truncated) at the same token budget.
    """

    fixture_id: str
    must_keep: frozenset[int]
    selected_curated: frozenset[int]
    selected_truncated: frozenset[int]
    parse_fallback: bool
    exceeded_budget: bool
    curated_answer: str
    truncated_answer: str
    curated_identified: bool
    truncated_identified: bool
    curation_latency_seconds: float
    prompt_tokens: int
    tags: frozenset[str]

    @property
    def curated_recall(self) -> bool:
        """Whether the curator's own selection kept every labelled `must_keep`
        event — the gate's own ground-truth recall check (secondary metric, issue
        #14's own "fraction of curated sets containing the labelled must-keep
        events").
        """
        return self.must_keep <= self.selected_curated

    @property
    def truncated_recall(self) -> bool:
        """The identical check against the baseline's own selection — reported for
        contrast, never gated (the baseline is free to lose a must-keep event; that
        is exactly the failure mode this experiment exists to beat)."""
        return self.must_keep <= self.selected_truncated


@dataclass(frozen=True, slots=True)
class CurationReport:
    """One curation bench run's full report — mirrors `TriageReport`'s metadata
    block, plus the curation-specific metrics issue #14's own gate
    (`heart.eval.curation_report`) is checked against.
    """

    model_repo: str
    prompt_variant: str
    chat_template: bool
    brain_model: str
    budget_fraction: float
    generated_at: str
    host: str
    commit: str | None
    branch: str | None
    mlx_lm_version: str | None
    max_output_tokens: int
    results: tuple[CurationFixtureResult, ...]

    must_keep_recall_rate: float
    curated_identified_count: int
    truncated_identified_count: int
    parse_fallback_rate: float
    exceeded_budget_count: int
    latency_p50_seconds: float
    latency_p95_seconds: float
    prompt_tokens_min: int
    prompt_tokens_median: float
    prompt_tokens_max: int
    peak_rss_bytes: int

    @property
    def fixture_count(self) -> int:
        return len(self.results)


def _render_selection(events: Sequence[CurationEvent], selected: frozenset[int]) -> str:
    """Renders `selected` back into chronological order — a selection is a set of
    sequence numbers with no inherent order of its own; the Brain reads a transcript,
    not a bag of lines.
    """
    ordered = sorted(
        (event for event in events if event.sequence in selected),
        key=lambda event: event.sequence,
    )
    return "\n".join(render_event(event) for event in ordered)


_WORD_PATTERN = re.compile(r"[a-z0-9']+")


def _identifies_failure_cause(answer: str, failure_cause: str) -> bool:
    """See module docstring's own proxy caveat. Matches the whole `failure_cause`
    label verbatim (case-insensitive) first; if that exact phrase isn't present — a
    paraphrased answer would otherwise always miss — falls back to requiring every
    content word (len > 2, so "a"/"to"/"it" don't trivially pass) of the label to
    appear somewhere in the answer.
    """
    answer_lower = answer.lower()
    cause_lower = failure_cause.lower().strip()
    if not cause_lower:
        return False
    if cause_lower in answer_lower:
        return True
    words = [word for word in _WORD_PATTERN.findall(cause_lower) if len(word) > 2]
    if not words:
        return False
    return all(word in answer_lower for word in words)


async def _ask_brain(
    brain: BrainProvider, *, transcript: str, model: str, max_output_tokens: int
) -> str:
    """One `BrainProvider.stream()` round trip, collecting the full text answer.
    `BrainProvider.stream()` never throws after invocation (issue #12's own
    invariant) — a `BrainErrorEvent` is rendered into the answer text itself, not
    raised, so a Brain-side failure on one fixture/condition never aborts the whole
    bench run; it just shows up as an obviously-failed answer in the report for a
    human to see.
    """
    request = BrainRequest(
        messages=(
            BrainMessage(
                role="user",
                content=FAILURE_QUESTION_TEMPLATE.format(transcript=transcript),
            ),
        ),
        model=model,
        max_output_tokens=max_output_tokens,
    )
    parts: list[str] = []
    gen = brain.stream(request)
    try:
        async for event in gen:
            if isinstance(event, BrainTextDelta):
                parts.append(event.text)
            elif isinstance(event, BrainErrorEvent):
                return f"(brain error: {event.message})"
    finally:
        await gen.aclose()
    return "".join(parts)


async def run_curation_bench(
    curator: ContextCurator,
    brain: BrainProvider,
    fixtures: Sequence[CurationFixture],
    *,
    model_repo: str,
    prompt_variant: str,
    brain_model: str,
    max_output_tokens: int,
    token_count: Callable[[str], int],
    budget_fraction: float = DEFAULT_BUDGET_FRACTION,
    chat_template: bool = False,
    commit: str | None = None,
    branch: str | None = None,
    perf_counter: Callable[[], float] = time.perf_counter,
) -> CurationReport:
    """Run every fixture in `fixtures` through both conditions and return the full
    report.

    `budget_tokens` for each fixture is computed as `budget_fraction` of that
    fixture's own full-transcript token count (via `token_count`, the Heart's own
    tokenizer — the curator is a Heart call, not a Brain one) — deliberately well
    under the full transcript, since a budget that fits everything makes curation
    vs. truncation degenerate (both conditions would simply keep every event).
    `curator` must already be backed by a loaded `HeartRuntime`, the same division of
    responsibility `heart.eval.runner.run_bench`/`heart.triage.HeartRequestTriage`
    already document.
    """
    peak_rss_bytes = read_peak_rss_bytes()

    results: list[CurationFixtureResult] = []
    for fixture in fixtures:
        full_tokens = sum(token_count(render_event(e)) for e in fixture.events)
        budget_tokens = max(1, math.floor(budget_fraction * full_tokens))

        start = perf_counter()
        outcome = curator.curate(fixture.events, budget_tokens=budget_tokens)
        curation_latency = perf_counter() - start

        truncated_selected = recency_window_selection(
            fixture.events, budget_tokens=budget_tokens, token_count=token_count
        )

        curated_answer = await _ask_brain(
            brain,
            transcript=_render_selection(fixture.events, outcome.selected),
            model=brain_model,
            max_output_tokens=max_output_tokens,
        )
        truncated_answer = await _ask_brain(
            brain,
            transcript=_render_selection(fixture.events, truncated_selected),
            model=brain_model,
            max_output_tokens=max_output_tokens,
        )

        results.append(
            CurationFixtureResult(
                fixture_id=fixture.id,
                must_keep=fixture.must_keep,
                selected_curated=outcome.selected,
                selected_truncated=truncated_selected,
                parse_fallback=outcome.parse_fallback,
                exceeded_budget=outcome.exceeded_budget,
                curated_answer=curated_answer,
                truncated_answer=truncated_answer,
                curated_identified=_identifies_failure_cause(
                    curated_answer, fixture.failure_cause
                ),
                truncated_identified=_identifies_failure_cause(
                    truncated_answer, fixture.failure_cause
                ),
                curation_latency_seconds=curation_latency,
                prompt_tokens=outcome.prompt_tokens,
                tags=fixture.tags,
            )
        )

    return _build_report(
        results,
        model_repo=model_repo,
        prompt_variant=prompt_variant,
        brain_model=brain_model,
        max_output_tokens=max_output_tokens,
        budget_fraction=budget_fraction,
        chat_template=chat_template,
        commit=commit,
        branch=branch,
        peak_rss_bytes=peak_rss_bytes,
    )


def _build_report(
    results: Sequence[CurationFixtureResult],
    *,
    model_repo: str,
    prompt_variant: str,
    brain_model: str,
    max_output_tokens: int,
    budget_fraction: float,
    chat_template: bool,
    commit: str | None = None,
    branch: str | None = None,
    peak_rss_bytes: int,
) -> CurationReport:
    fallback_count = sum(1 for r in results if r.parse_fallback)
    exceeded_count = sum(1 for r in results if r.exceeded_budget)
    recall_count = sum(1 for r in results if r.curated_recall)
    curated_identified_count = sum(1 for r in results if r.curated_identified)
    truncated_identified_count = sum(1 for r in results if r.truncated_identified)

    latencies = [r.curation_latency_seconds for r in results]
    prompt_tokens = [r.prompt_tokens for r in results]

    return CurationReport(
        model_repo=model_repo,
        prompt_variant=prompt_variant,
        chat_template=chat_template,
        brain_model=brain_model,
        budget_fraction=budget_fraction,
        generated_at=datetime.now(UTC).isoformat(),
        host=host_platform(),
        commit=commit,
        branch=branch,
        mlx_lm_version=mlx_lm_version(),
        max_output_tokens=max_output_tokens,
        results=tuple(results),
        must_keep_recall_rate=recall_count / len(results),
        curated_identified_count=curated_identified_count,
        truncated_identified_count=truncated_identified_count,
        parse_fallback_rate=fallback_count / len(results),
        exceeded_budget_count=exceeded_count,
        latency_p50_seconds=percentile(latencies, 0.50),
        latency_p95_seconds=percentile(latencies, 0.95),
        prompt_tokens_min=min(prompt_tokens),
        prompt_tokens_median=statistics.median(prompt_tokens),
        prompt_tokens_max=max(prompt_tokens),
        peak_rss_bytes=peak_rss_bytes,
    )
