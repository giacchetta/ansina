"""Label-agnostic measurement primitives shared by every `heart.eval` bench suite.

Issue #13 adds a second bench suite (request triage) alongside #53's tick-decision
one; this module is where the primitives that don't care *which* label enum a suite
classifies against live, so the two suites extend the harness rather than duplicate
it. `heart.eval.runner`/`report.py` (tick) and `heart.eval.triage_runner`/
`triage_report.py` (triage) both build on this.

Deliberately *not* shared: report rendering (`report_to_markdown`/`report_to_json` and
their triage counterparts). The two reports are genuinely different documents —
different gate clauses, different metrics, different class names — and only their
~10-line metadata header looks alike; parameterising that over a shared renderer would
cost more readability than the duplication it would save.
"""

from __future__ import annotations

import platform
import resource
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from importlib import metadata
from types import MappingProxyType
from typing import Protocol


def percentile(values: Sequence[float], percentile_rank: float) -> float:
    """Nearest-rank percentile over `values`, sorted ascending. `values` must be
    non-empty — callers only reach this once at least one fixture has run.
    """
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0, min(len(ordered) - 1, round(percentile_rank * (len(ordered) - 1))))
    return ordered[rank]


def read_peak_rss_bytes() -> int:
    """Peak RSS in bytes for this process, platform-corrected.

    `ru_maxrss` is bytes on macOS/BSD and KiB on Linux — a `str`-typed local, not a
    direct `sys.platform ==` comparison, for the same mypy-unreachable-branch reason
    `heart/selection.py`'s `_mlx_viable` documents (CI runs both `ubuntu-24.04` and
    `macos-26`, so a direct comparison would statically strand one leg's branch).
    """
    peak_rss_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    current_platform: str = sys.platform
    if current_platform != "darwin":
        peak_rss_bytes *= 1024
    return peak_rss_bytes


def mlx_lm_version() -> str | None:
    try:
        return metadata.version("mlx-lm")
    except metadata.PackageNotFoundError:
        return None


def host_platform() -> str:
    return platform.platform()


class ClassifiedOutcome[LabelT: StrEnum](Protocol):
    """The shape `label_metrics` needs from one fixture result — structural, so
    `heart.eval.runner.FixtureResult` and
    `heart.eval.triage_runner.TriageFixtureResult` both satisfy it with no
    inheritance relationship between the two.

    `expected` is declared as a read-only `@property`, not a plain attribute: a
    plain-attribute Protocol member requires a *settable* field, which a frozen
    dataclass's own field (read-only by construction) can never satisfy — both
    concrete classes above are `@dataclass(frozen=True)`.
    """

    @property
    def expected(self) -> LabelT: ...

    @property
    def correct(self) -> bool: ...

    @property
    def parse_fallback(self) -> bool: ...

    @property
    def tags(self) -> frozenset[str]: ...


@dataclass(frozen=True, slots=True)
class LabelMetrics[LabelT: StrEnum]:
    """Accuracy/recall/parse-fallback metrics computed over one label enum's worth of
    `ClassifiedOutcome`s — generic over which enum, since `label_metrics` below takes
    it as a parameter rather than hardcoding `TickDecision` or `TriageClass`.

    `recall_by_label`/`label_counts` are keyed by the enum *member* itself, not its
    `.value` string — the same typing `heart.eval.runner.BenchReport.recall_by_class`
    already carries (`Mapping[TickDecision, float]`), preserved here rather than
    narrowed to `str` so callers keep indexing by enum member, not a string literal.
    """

    accuracy: float
    recall_by_label: Mapping[LabelT, float]
    label_counts: Mapping[LabelT, int]
    recall_by_tag: Mapping[str, float]
    tag_counts: Mapping[str, int]
    parse_fallback_rate: float


def label_metrics[LabelT: StrEnum](
    results: Sequence[ClassifiedOutcome[LabelT]], *, labels: type[LabelT]
) -> LabelMetrics[LabelT]:
    """Compute accuracy, per-label recall, per-tag recall, and the parse-fallback
    rate over `results` — the one place either bench suite defines what these mean,
    so `heart.eval.report`/`triage_report` can't disagree with `runner`/
    `triage_runner` about how a metric is derived. `results` must be non-empty.
    """
    correct = sum(1 for r in results if r.correct)
    accuracy = correct / len(results)

    label_counts: dict[LabelT, int] = {label: 0 for label in labels}
    label_correct: dict[LabelT, int] = {label: 0 for label in labels}
    for result in results:
        label_counts[result.expected] += 1
        if result.correct:
            label_correct[result.expected] += 1
    recall_by_label = {
        label: (label_correct[label] / count if count else 0.0)
        for label, count in label_counts.items()
    }

    all_tags = sorted({tag for r in results for tag in r.tags})
    tag_counts = {tag: sum(1 for r in results if tag in r.tags) for tag in all_tags}
    tag_correct = {
        tag: sum(1 for r in results if tag in r.tags and r.correct) for tag in all_tags
    }
    recall_by_tag = {
        tag: (tag_correct[tag] / tag_counts[tag] if tag_counts[tag] else 0.0)
        for tag in all_tags
    }

    fallback_count = sum(1 for r in results if r.parse_fallback)

    return LabelMetrics(
        accuracy=accuracy,
        recall_by_label=MappingProxyType(recall_by_label),
        label_counts=MappingProxyType(label_counts),
        recall_by_tag=MappingProxyType(recall_by_tag),
        tag_counts=MappingProxyType(tag_counts),
        parse_fallback_rate=fallback_count / len(results),
    )
