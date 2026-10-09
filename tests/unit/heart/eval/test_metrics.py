from __future__ import annotations

from importlib import metadata

import pytest

from ansina.heart.eval.metrics import (
    host_platform,
    label_metrics,
    mlx_lm_version,
    percentile,
    read_peak_rss_bytes,
)
from ansina.heart.eval.runner import FixtureResult
from ansina.heart.tick.decision import TickDecision


def _result(
    expected: TickDecision,
    actual: TickDecision | None,
    *,
    tags: frozenset[str] = frozenset(),
) -> FixtureResult:
    return FixtureResult(
        fixture_id="f",
        expected=expected,
        actual=actual,
        raw_output=actual.value if actual is not None else "???",
        latency_seconds=0.0,
        prompt_tokens=1,
        tags=tags,
    )


# --- percentile --------------------------------------------------------------------


def test_percentile_returns_the_only_value_when_given_one() -> None:
    assert percentile([5.0], 0.95) == 5.0


def test_percentile_nearest_rank_over_several_values() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert percentile(values, 0.0) == 1.0
    assert percentile(values, 1.0) == 5.0
    assert percentile(values, 0.5) == 3.0


# --- read_peak_rss_bytes ------------------------------------------------------------


class _FakeRusage:
    def __init__(self, ru_maxrss: int) -> None:
        self.ru_maxrss = ru_maxrss


def test_read_peak_rss_bytes_leaves_rss_unscaled_on_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ru_maxrss` is already bytes on macOS/BSD — forced via monkeypatch so this
    holds regardless of which OS actually runs the suite, the same reasoning
    `tests/unit/heart/test_selection.py` documents for `_mlx_viable`.
    """
    monkeypatch.setattr("ansina.heart.eval.metrics.sys.platform", "darwin")
    monkeypatch.setattr(
        "ansina.heart.eval.metrics.resource.getrusage", lambda _who: _FakeRusage(1000)
    )

    assert read_peak_rss_bytes() == 1000


def test_read_peak_rss_bytes_scales_from_kib_to_bytes_off_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ru_maxrss` is KiB on Linux — forced via monkeypatch for the same reason as
    the darwin case above, so both branches are covered regardless of host OS.
    """
    monkeypatch.setattr("ansina.heart.eval.metrics.sys.platform", "linux")
    monkeypatch.setattr(
        "ansina.heart.eval.metrics.resource.getrusage", lambda _who: _FakeRusage(1000)
    )

    assert read_peak_rss_bytes() == 1000 * 1024


# --- mlx_lm_version ------------------------------------------------------------------


def test_mlx_lm_version_returns_the_installed_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ansina.heart.eval.metrics.metadata.version", lambda _name: "0.31.3"
    )

    assert mlx_lm_version() == "0.31.3"


def test_mlx_lm_version_returns_none_when_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_name: str) -> str:
        raise metadata.PackageNotFoundError("mlx-lm")

    monkeypatch.setattr("ansina.heart.eval.metrics.metadata.version", _raise)

    assert mlx_lm_version() is None


# --- host_platform -------------------------------------------------------------------


def test_host_platform_returns_a_non_empty_string() -> None:
    assert isinstance(host_platform(), str)
    assert host_platform()


# --- label_metrics -------------------------------------------------------------------


def test_label_metrics_scores_every_result_correct() -> None:
    results = [
        _result(TickDecision.IDLE, TickDecision.IDLE),
        _result(TickDecision.ACT, TickDecision.ACT),
        _result(TickDecision.ESCALATE, TickDecision.ESCALATE),
    ]

    metrics = label_metrics(results, labels=TickDecision)

    assert metrics.accuracy == 1.0
    assert metrics.parse_fallback_rate == 0.0
    for decision in TickDecision:
        assert metrics.recall_by_label[decision] == 1.0
        assert metrics.label_counts[decision] == 1
    assert dict(metrics.recall_by_tag) == {}
    assert dict(metrics.tag_counts) == {}


def test_label_metrics_computes_recall_and_fallback_on_a_mixed_set() -> None:
    results = [
        _result(TickDecision.IDLE, TickDecision.IDLE, tags=frozenset({"self_state"})),
        _result(TickDecision.IDLE, TickDecision.ACT, tags=frozenset({"self_state"})),
        _result(TickDecision.ACT, None, tags=frozenset({"self_state_fault"})),
        _result(TickDecision.ESCALATE, TickDecision.ESCALATE),
    ]

    metrics = label_metrics(results, labels=TickDecision)

    assert metrics.accuracy == pytest.approx(2 / 4)
    assert metrics.parse_fallback_rate == pytest.approx(1 / 4)
    assert metrics.recall_by_label[TickDecision.IDLE] == pytest.approx(1 / 2)
    assert metrics.recall_by_label[TickDecision.ACT] == 0.0
    assert metrics.recall_by_label[TickDecision.ESCALATE] == 1.0
    assert metrics.label_counts[TickDecision.IDLE] == 2
    assert metrics.label_counts[TickDecision.ACT] == 1
    assert metrics.label_counts[TickDecision.ESCALATE] == 1
    assert metrics.recall_by_tag["self_state"] == pytest.approx(1 / 2)
    assert metrics.recall_by_tag["self_state_fault"] == 0.0
    assert metrics.tag_counts["self_state"] == 2
    assert metrics.tag_counts["self_state_fault"] == 1


def test_label_metrics_gives_zero_recall_for_an_unseen_label() -> None:
    """A label enum member with zero fixtures expecting it must still appear in
    `recall_by_label`/`label_counts` (as 0.0/0) rather than being absent — the
    report renderer iterates the full enum unconditionally.
    """
    results = [_result(TickDecision.IDLE, TickDecision.IDLE)]

    metrics = label_metrics(results, labels=TickDecision)

    assert metrics.recall_by_label[TickDecision.ACT] == 0.0
    assert metrics.label_counts[TickDecision.ACT] == 0
