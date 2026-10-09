"""Runs the triage fixture set against a `RequestTriage` port and computes the triage
bench report. See issue #13.

Mirrors `heart.eval.runner`'s own shape closely (`FixtureResult`/`run_bench`/
`_build_report` -> `TriageFixtureResult`/`run_triage_bench`/`_build_report` here),
extending the harness rather than duplicating its label-agnostic measurement — see
`heart.eval.metrics`. The one structural difference: this bench takes the **port**
(`heart.triage.RequestTriage`), not a bare `HeartRuntime` — the triage bench measures
the shipped port end to end (prompt rendering, parsing, and all), the same way a tick
fixture is rendered through the real `heart.tick.snapshot.build_prompt` rather than a
parallel copy of it.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from ansina.heart.eval.fixtures import TriageFixture
from ansina.heart.eval.metrics import (
    host_platform,
    label_metrics,
    mlx_lm_version,
    percentile,
    read_peak_rss_bytes,
)
from ansina.heart.triage import RequestTriage, TriageClass, routes_to_brain


@dataclass(frozen=True, slots=True)
class TriageFixtureResult:
    """The raw outcome of running one triage fixture. `actual` is `None` iff
    `heart.triage.try_parse_triage` couldn't recognize the port's reply — a parse
    fallback, tracked separately from a correct or incorrect classification, the same
    distinction `heart.eval.runner.FixtureResult` draws for tick decisions.
    """

    fixture_id: str
    expected: TriageClass
    actual: TriageClass | None
    raw_output: str
    latency_seconds: float
    prompt_tokens: int
    tags: frozenset[str]

    @property
    def correct(self) -> bool:
        return self.actual == self.expected

    @property
    def parse_fallback(self) -> bool:
        return self.actual is None

    @property
    def routed_to_brain(self) -> bool:
        """Whether the *actual* classification would route to the Brain under
        today's 2-way routing — `None` (a parse fallback) is never mistaken for a
        real `TRIVIAL` answer, so it routes to the Brain too (the safe default
        `heart.triage.parse_triage` itself falls back to).
        """
        return self.actual is None or routes_to_brain(self.actual)

    @property
    def under_route(self) -> bool:
        """`expected` needed the Brain but `actual` would have answered directly —
        the dangerous direction issue #13's own gate names a zero-tolerance ceiling
        on.
        """
        return routes_to_brain(self.expected) and not self.routed_to_brain

    @property
    def over_route(self) -> bool:
        """`expected` was answerable directly but `actual` would have escalated
        anyway — an efficiency loss, not a correctness bug; tolerated up to a stated
        ceiling, not zero.
        """
        return not routes_to_brain(self.expected) and self.routed_to_brain


@dataclass(frozen=True, slots=True)
class TriageReport:
    """One triage bench run's full report — mirrors `heart.eval.runner.BenchReport`'s
    metadata block, plus the triage-specific metrics issue #13's own gate (see
    `heart.eval.triage_report`) is checked against.
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
    results: tuple[TriageFixtureResult, ...]

    accuracy: float
    recall_by_class: Mapping[TriageClass, float]
    class_counts: Mapping[TriageClass, int]
    recall_by_tag: Mapping[str, float]
    tag_counts: Mapping[str, int]
    parse_fallback_rate: float
    # The two misroute directions, kept separate rather than summed — they are not
    # equally bad (issue #13's own "Accept/reject metric" section). `complex`-> trivial
    # is the zero-tolerance correctness bug; `tool-only`->`trivial` is the same bug
    # under today's 2-way routing, where `tool-only` has no executor and so routes to
    # the Brain exactly like `complex` does.
    misroute_complex_to_trivial: int
    misroute_tool_only_to_trivial: int
    over_route_count: int
    over_route_rate: float
    # The primary cost-saved figure vs. the always-escalate baseline (which saves
    # exactly 0) — never gated, since it's the number the whole experiment exists to
    # report, not a pass/fail threshold.
    brain_calls_saved_fraction: float
    # `confusion[expected][actual_label]` -> count, `actual_label` being a
    # `TriageClass.value` or the literal string `"unparsed"` for a parse fallback —
    # the full picture `recall_by_class` alone only summarizes.
    confusion: Mapping[TriageClass, Mapping[str, int]]
    latency_p50_seconds: float
    latency_p95_seconds: float
    prompt_tokens_min: int
    prompt_tokens_median: float
    prompt_tokens_max: int
    peak_rss_bytes: int

    @property
    def fixture_count(self) -> int:
        return len(self.results)


def _build_confusion(
    results: Sequence[TriageFixtureResult],
) -> Mapping[TriageClass, Mapping[str, int]]:
    confusion: dict[TriageClass, dict[str, int]] = {
        label: {other.value: 0 for other in TriageClass} | {"unparsed": 0}
        for label in TriageClass
    }
    for result in results:
        actual_label = result.actual.value if result.actual is not None else "unparsed"
        confusion[result.expected][actual_label] += 1
    return {label: dict(row) for label, row in confusion.items()}


def run_triage_bench(
    triage: RequestTriage,
    fixtures: Sequence[TriageFixture],
    *,
    model_repo: str,
    prompt_variant: str,
    max_output_tokens: int,
    chat_template: bool = False,
    commit: str | None = None,
    branch: str | None = None,
    perf_counter: Callable[[], float] = time.perf_counter,
) -> TriageReport:
    """Run every fixture in `fixtures` against `triage` and return the full report.

    Takes the **port** (`heart.triage.RequestTriage`), not a bare `HeartRuntime` — the
    unit suite injects a scripted fake, `__main__.py` is the only caller that wires in
    the real `heart.triage.HeartRequestTriage` over a real `HeartRuntime`.
    `model_repo`/`prompt_variant`/`max_output_tokens`/`chat_template`/`commit`/
    `branch` are pure metadata recorded onto the report, the same division of
    responsibility `heart.eval.runner.run_bench` already documents for its own
    identically-named parameters. `perf_counter` is injectable for the same
    deterministic-latency-math reason.
    """
    peak_rss_bytes = read_peak_rss_bytes()

    results: list[TriageFixtureResult] = []
    for fixture in fixtures:
        start = perf_counter()
        outcome = triage.classify(fixture.request)
        latency = perf_counter() - start
        results.append(
            TriageFixtureResult(
                fixture_id=fixture.id,
                expected=fixture.expect,
                actual=outcome.parsed,
                raw_output=outcome.raw_output,
                latency_seconds=latency,
                prompt_tokens=outcome.prompt_tokens,
                tags=fixture.tags,
            )
        )

    return _build_report(
        results,
        model_repo=model_repo,
        prompt_variant=prompt_variant,
        max_output_tokens=max_output_tokens,
        chat_template=chat_template,
        commit=commit,
        branch=branch,
        peak_rss_bytes=peak_rss_bytes,
    )


def _build_report(
    results: Sequence[TriageFixtureResult],
    *,
    model_repo: str,
    prompt_variant: str,
    max_output_tokens: int,
    chat_template: bool,
    commit: str | None = None,
    branch: str | None = None,
    peak_rss_bytes: int,
) -> TriageReport:
    metrics = label_metrics(results, labels=TriageClass)

    misroute_complex_to_trivial = sum(
        1
        for r in results
        if r.expected is TriageClass.COMPLEX and r.actual is TriageClass.TRIVIAL
    )
    misroute_tool_only_to_trivial = sum(
        1
        for r in results
        if r.expected is TriageClass.TOOL_ONLY and r.actual is TriageClass.TRIVIAL
    )
    over_route_count = sum(1 for r in results if r.over_route)

    # Saved vs. the always-escalate baseline (issue #13's own "Baseline" section —
    # always escalate, saving exactly 0): a fixture saves a Brain call iff it did
    # *not* route to the Brain under the actual classification — an `over_route`d
    # fixture that still routed to the Brain saves nothing despite being "wrong".
    saved = sum(1 for r in results if not r.routed_to_brain)

    latencies = [r.latency_seconds for r in results]
    prompt_tokens = [r.prompt_tokens for r in results]

    return TriageReport(
        model_repo=model_repo,
        prompt_variant=prompt_variant,
        chat_template=chat_template,
        generated_at=datetime.now(UTC).isoformat(),
        host=host_platform(),
        commit=commit,
        branch=branch,
        mlx_lm_version=mlx_lm_version(),
        max_output_tokens=max_output_tokens,
        results=tuple(results),
        accuracy=metrics.accuracy,
        recall_by_class=metrics.recall_by_label,
        class_counts=metrics.label_counts,
        recall_by_tag=metrics.recall_by_tag,
        tag_counts=metrics.tag_counts,
        parse_fallback_rate=metrics.parse_fallback_rate,
        misroute_complex_to_trivial=misroute_complex_to_trivial,
        misroute_tool_only_to_trivial=misroute_tool_only_to_trivial,
        over_route_count=over_route_count,
        over_route_rate=over_route_count / len(results),
        brain_calls_saved_fraction=saved / len(results),
        confusion=_build_confusion(results),
        latency_p50_seconds=percentile(latencies, 0.50),
        latency_p95_seconds=percentile(latencies, 0.95),
        prompt_tokens_min=min(prompt_tokens),
        prompt_tokens_median=statistics.median(prompt_tokens),
        prompt_tokens_max=max(prompt_tokens),
        peak_rss_bytes=peak_rss_bytes,
    )
