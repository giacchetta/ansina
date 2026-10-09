"""Runs the tick fixture set against a `HeartRuntime` and computes the bench report.
See issue #53.

Every fixture is rendered through the actual shipped `heart.tick.snapshot.build_prompt`
and parsed through `heart.tick.decision.try_parse_decision` — never a parallel copy of
either. `run_bench` takes any `HeartRuntime`, so the unit suite injects a fake
(mirroring `tests/unit/heart/tick/test_loop.py`'s `_FakeHeart`) and never touches the
network or a real model; `__main__.py` is the only caller that passes the real MLX
adapter.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from ansina.heart.eval.fixtures import TickFixture
from ansina.heart.eval.metrics import (
    host_platform,
    label_metrics,
    mlx_lm_version,
    percentile,
    read_peak_rss_bytes,
)
from ansina.heart.runtime import HeartRuntime
from ansina.heart.tick.decision import TickDecision, try_parse_decision
from ansina.heart.tick.prompts import DEFAULT_PROMPT_VARIANT, DEFAULT_TEMPLATE
from ansina.heart.tick.snapshot import build_prompt

OBVIOUSLY_IDLE_TAG = "obviously_idle"


@dataclass(frozen=True, slots=True)
class FixtureResult:
    """The raw outcome of running one fixture. `actual` is `None` iff
    `try_parse_decision` couldn't recognize the model's reply — a parse fallback,
    tracked separately from a correct or incorrect decision.
    """

    fixture_id: str
    expected: TickDecision
    actual: TickDecision | None
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
    def false_act_or_escalate(self) -> bool:
        """A wrong `act`/`escalate` call on a fixture expecting `idle` — the gate's
        "zero false act/escalate on the obviously-idle subset" clause is this, scoped
        to `OBVIOUSLY_IDLE_TAG`-tagged fixtures.
        """
        return self.actual in (TickDecision.ACT, TickDecision.ESCALATE)


@dataclass(frozen=True, slots=True)
class BenchReport:
    """One bench run's full report: metadata, raw per-fixture results, and the
    aggregated metrics issue #53's gate is checked against — computed here, not left
    for a caller to derive, so `report.py` and `__main__.py` can't disagree with each
    other about how a metric is defined.
    """

    model_repo: str
    prompt_variant: str
    chat_template: bool
    generated_at: str
    host: str
    # Issue #58: the git commit (`-dirty`-suffixed if the worktree had uncommitted
    # changes) and branch the bench was run against, from
    # `ansina.heart.eval.provenance.resolve_provenance` — `None`/`None` together
    # when it couldn't be determined (no `git`, not a checkout). Metadata only,
    # never a gate input.
    commit: str | None
    branch: str | None
    mlx_lm_version: str | None
    max_output_tokens: int
    results: tuple[FixtureResult, ...]

    accuracy: float
    recall_by_class: Mapping[TickDecision, float]
    class_counts: Mapping[TickDecision, int]
    # Issue #54: recall scoped to each fixture *tag* seen across the set (e.g.
    # "obviously_idle", "self_state", "self_state_fault") — not a gate clause, unlike
    # `recall_by_class` above. This is how the fault-injection AC's "measured rate" is a
    # reproducible number in the committed report rather than a hand-written note: the
    # `self_state_fault` row is that rate.
    recall_by_tag: Mapping[str, float]
    tag_counts: Mapping[str, int]
    parse_fallback_rate: float
    false_act_or_escalate_on_obvious_idle: int
    latency_p50_seconds: float
    latency_p95_seconds: float
    prompt_tokens_min: int
    prompt_tokens_median: float
    prompt_tokens_max: int
    peak_rss_bytes: int

    @property
    def fixture_count(self) -> int:
        return len(self.results)


def run_bench(
    runtime: HeartRuntime,
    fixtures: Sequence[TickFixture],
    *,
    budget_tokens: int,
    max_output_tokens: int,
    template: str = DEFAULT_TEMPLATE,
    prompt_variant: str = DEFAULT_PROMPT_VARIANT,
    model_repo: str,
    chat_template: bool = False,
    commit: str | None = None,
    branch: str | None = None,
    perf_counter: Callable[[], float] = time.perf_counter,
) -> BenchReport:
    """Run every fixture in `fixtures` against `runtime` and return the full report.

    `runtime` must already be loaded (`runtime.load()`) — this function never calls it,
    so `peak_rss_bytes` (measured at the start of this call) reflects RSS after load,
    per issue #53's own metric list, regardless of whatever else the caller does first.
    `chat_template` is pure metadata recorded onto the report — whether the prompt was
    actually wrapped in the tokenizer's chat template happened (or didn't) inside
    `runtime.generate` itself, this function has no say in it. `commit`/`branch`
    (issue #58) are likewise pure metadata, defaulted to `None` so every pre-#58 caller
    is unaffected. `perf_counter` is injectable so the unit suite can assert on latency
    math without real timing variance; it defaults to `time.perf_counter`.
    """
    peak_rss_bytes = read_peak_rss_bytes()

    results: list[FixtureResult] = []
    for fixture in fixtures:
        prompt = build_prompt(
            list(fixture.items),
            budget_tokens=budget_tokens,
            token_count=runtime.token_count,
            template=template,
        )
        start = perf_counter()
        raw = runtime.generate(prompt.text, max_tokens=max_output_tokens)
        latency = perf_counter() - start
        actual = try_parse_decision(raw)
        results.append(
            FixtureResult(
                fixture_id=fixture.id,
                expected=fixture.expect,
                actual=actual,
                raw_output=raw,
                latency_seconds=latency,
                prompt_tokens=prompt.tokens,
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
    results: Sequence[FixtureResult],
    *,
    model_repo: str,
    prompt_variant: str,
    max_output_tokens: int,
    chat_template: bool,
    commit: str | None = None,
    branch: str | None = None,
    peak_rss_bytes: int,
) -> BenchReport:
    metrics = label_metrics(results, labels=TickDecision)

    false_on_obvious_idle = sum(
        1 for r in results if OBVIOUSLY_IDLE_TAG in r.tags and r.false_act_or_escalate
    )

    latencies = [r.latency_seconds for r in results]
    prompt_tokens = [r.prompt_tokens for r in results]

    return BenchReport(
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
        false_act_or_escalate_on_obvious_idle=false_on_obvious_idle,
        latency_p50_seconds=percentile(latencies, 0.50),
        latency_p95_seconds=percentile(latencies, 0.95),
        prompt_tokens_min=min(prompt_tokens),
        prompt_tokens_median=statistics.median(prompt_tokens),
        prompt_tokens_max=max(prompt_tokens),
        peak_rss_bytes=peak_rss_bytes,
    )
