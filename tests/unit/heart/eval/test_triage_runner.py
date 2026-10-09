from __future__ import annotations

from collections.abc import Sequence

import pytest

from ansina.heart.eval.fixtures import TriageFixture
from ansina.heart.eval.triage_runner import (
    TriageFixtureResult,
    run_triage_bench,
)
from ansina.heart.triage import RequestTriage, TriageClass, TriageOutcome


class _ScriptedTriage(RequestTriage):
    """A `RequestTriage` fake scripted with one reply per call, falling back to the
    last one once exhausted — the triage-bench counterpart of `test_runner.py`'s own
    `_FakeHeart`.
    """

    def __init__(
        self, *, replies: Sequence[TriageClass | None], prompt_tokens: int = 10
    ) -> None:
        self._replies = list(replies)
        self._prompt_tokens = prompt_tokens
        self.calls = 0
        self.requests: list[str] = []

    def classify(self, request: str) -> TriageOutcome:
        self.requests.append(request)
        index = min(self.calls, len(self._replies) - 1)
        self.calls += 1
        parsed = self._replies[index]
        return TriageOutcome(
            request=request,
            triage_class=parsed if parsed is not None else TriageClass.COMPLEX,
            parsed=parsed,
            raw_output=parsed.value if parsed is not None else "???",
            prompt_tokens=self._prompt_tokens,
        )


def _fixture(
    fixture_id: str, expect: TriageClass, *, tags: frozenset[str] = frozenset()
) -> TriageFixture:
    return TriageFixture(id=fixture_id, expect=expect, request="r", tags=tags)


def test_run_triage_bench_scores_every_fixture_correct() -> None:
    fixtures = [
        _fixture("t1", TriageClass.TRIVIAL),
        _fixture("o1", TriageClass.TOOL_ONLY),
        _fixture("c1", TriageClass.COMPLEX),
    ]
    triage = _ScriptedTriage(
        replies=[TriageClass.TRIVIAL, TriageClass.TOOL_ONLY, TriageClass.COMPLEX]
    )

    report = run_triage_bench(
        triage,
        fixtures,
        model_repo="fake/model",
        prompt_variant="strict",
        max_output_tokens=50,
    )

    assert report.accuracy == 1.0
    assert report.parse_fallback_rate == 0.0
    assert report.misroute_complex_to_trivial == 0
    assert report.misroute_tool_only_to_trivial == 0
    assert report.over_route_count == 0
    assert report.fixture_count == 3
    for triage_class in TriageClass:
        assert report.recall_by_class[triage_class] == 1.0
        assert report.class_counts[triage_class] == 1
    # trivial correctly answers directly; tool-only/complex both correctly escalate —
    # so this baseline-matching run saves exactly 1/3 of calls (the trivial one).
    assert report.brain_calls_saved_fraction == pytest.approx(1 / 3)


def test_run_triage_bench_flags_the_zero_tolerance_misroute_direction() -> None:
    """A `complex` fixture misrouted to `trivial` — answering directly what needed
    the Brain — is issue #13's own zero-tolerance reject condition.
    """
    fixtures = [_fixture("c1", TriageClass.COMPLEX)]
    triage = _ScriptedTriage(replies=[TriageClass.TRIVIAL])

    report = run_triage_bench(
        triage,
        fixtures,
        model_repo="fake/model",
        prompt_variant="strict",
        max_output_tokens=50,
    )

    assert report.misroute_complex_to_trivial == 1
    assert report.misroute_tool_only_to_trivial == 0
    assert report.results[0].under_route is True
    assert report.results[0].over_route is False


def test_run_triage_bench_flags_the_tool_only_misroute_direction() -> None:
    """Under today's 2-way routing, `tool-only` escalates exactly like `complex`
    does — misrouting it to `trivial` is the identical under-route bug.
    """
    fixtures = [_fixture("o1", TriageClass.TOOL_ONLY)]
    triage = _ScriptedTriage(replies=[TriageClass.TRIVIAL])

    report = run_triage_bench(
        triage,
        fixtures,
        model_repo="fake/model",
        prompt_variant="strict",
        max_output_tokens=50,
    )

    assert report.misroute_tool_only_to_trivial == 1
    assert report.misroute_complex_to_trivial == 0


def test_run_triage_bench_counts_an_over_route_without_flagging_a_misroute() -> None:
    """A `trivial` fixture classified as `complex` is an efficiency loss (an
    unnecessary Brain call), not a correctness bug — never counted as a misroute.
    """
    fixtures = [_fixture("t1", TriageClass.TRIVIAL)]
    triage = _ScriptedTriage(replies=[TriageClass.COMPLEX])

    report = run_triage_bench(
        triage,
        fixtures,
        model_repo="fake/model",
        prompt_variant="strict",
        max_output_tokens=50,
    )

    assert report.over_route_count == 1
    assert report.over_route_rate == 1.0
    assert report.misroute_complex_to_trivial == 0
    assert report.misroute_tool_only_to_trivial == 0
    assert report.results[0].over_route is True
    assert report.results[0].under_route is False
    # Over-routed but still escalated — saves nothing despite being "wrong".
    assert report.brain_calls_saved_fraction == 0.0


def test_run_triage_bench_counts_a_parse_fallback_separately_from_correct_complex() -> (
    None
):
    fixtures = [_fixture("c1", TriageClass.COMPLEX)]
    triage = _ScriptedTriage(replies=[None])

    report = run_triage_bench(
        triage,
        fixtures,
        model_repo="fake/model",
        prompt_variant="strict",
        max_output_tokens=50,
    )

    assert report.accuracy == 0.0
    assert report.parse_fallback_rate == 1.0
    assert report.results[0].actual is None
    assert report.results[0].parse_fallback is True
    assert report.results[0].correct is False
    # A parse fallback never counts as a dangerous misroute (expected != actual is
    # never True when actual is None) even though it does route to the Brain.
    assert report.misroute_complex_to_trivial == 0
    assert report.results[0].routed_to_brain is True


def test_run_triage_bench_treats_a_parse_fallback_as_routing_to_the_brain() -> None:
    """A parse fallback on a `trivial` fixture must not look like an under-route —
    the safe default (always escalate on an unparseable reply) routes to the Brain,
    so it's an over-route (an efficiency loss), never a correctness bug.
    """
    fixtures = [_fixture("t1", TriageClass.TRIVIAL)]
    triage = _ScriptedTriage(replies=[None])

    report = run_triage_bench(
        triage,
        fixtures,
        model_repo="fake/model",
        prompt_variant="strict",
        max_output_tokens=50,
    )

    assert report.results[0].routed_to_brain is True
    assert report.results[0].over_route is True
    assert report.results[0].under_route is False


def test_run_triage_bench_builds_the_confusion_matrix() -> None:
    fixtures = [
        _fixture("t1", TriageClass.TRIVIAL),
        _fixture("t2", TriageClass.TRIVIAL),
        _fixture("c1", TriageClass.COMPLEX),
    ]
    triage = _ScriptedTriage(replies=[TriageClass.TRIVIAL, TriageClass.COMPLEX, None])

    report = run_triage_bench(
        triage,
        fixtures,
        model_repo="fake/model",
        prompt_variant="strict",
        max_output_tokens=50,
    )

    assert report.confusion[TriageClass.TRIVIAL]["trivial"] == 1
    assert report.confusion[TriageClass.TRIVIAL]["complex"] == 1
    assert report.confusion[TriageClass.TRIVIAL]["unparsed"] == 0
    assert report.confusion[TriageClass.COMPLEX]["unparsed"] == 1
    assert report.confusion[TriageClass.TOOL_ONLY] == {
        "trivial": 0,
        "tool-only": 0,
        "complex": 0,
        "unparsed": 0,
    }


def test_run_triage_bench_computes_recall_by_tag() -> None:
    fixtures = [
        _fixture("t1", TriageClass.TRIVIAL, tags=frozenset({"obviously_trivial"})),
        _fixture("t2", TriageClass.TRIVIAL, tags=frozenset({"ambiguous"})),
    ]
    triage = _ScriptedTriage(replies=[TriageClass.TRIVIAL, TriageClass.COMPLEX])

    report = run_triage_bench(
        triage,
        fixtures,
        model_repo="fake/model",
        prompt_variant="strict",
        max_output_tokens=50,
    )

    assert report.tag_counts == {"ambiguous": 1, "obviously_trivial": 1}
    assert report.recall_by_tag["obviously_trivial"] == 1.0
    assert report.recall_by_tag["ambiguous"] == 0.0


def test_run_triage_bench_uses_the_injected_perf_counter_for_latency() -> None:
    fixtures = [
        _fixture("t1", TriageClass.TRIVIAL),
        _fixture("t2", TriageClass.TRIVIAL),
    ]
    triage = _ScriptedTriage(replies=[TriageClass.TRIVIAL, TriageClass.TRIVIAL])
    calls = iter([0.0, 1.0, 1.0, 3.0])

    report = run_triage_bench(
        triage,
        fixtures,
        model_repo="fake/model",
        prompt_variant="strict",
        max_output_tokens=50,
        perf_counter=lambda: next(calls),
    )

    assert [r.latency_seconds for r in report.results] == [1.0, 2.0]
    assert report.latency_p95_seconds == 2.0


def test_run_triage_bench_records_metadata() -> None:
    fixtures = [_fixture("t1", TriageClass.TRIVIAL)]
    triage = _ScriptedTriage(replies=[TriageClass.TRIVIAL])

    report = run_triage_bench(
        triage,
        fixtures,
        model_repo="fake/model",
        prompt_variant="strict",
        max_output_tokens=42,
        chat_template=True,
        commit="abc1234",
        branch="m7-first-brain",
    )

    assert report.model_repo == "fake/model"
    assert report.prompt_variant == "strict"
    assert report.chat_template is True
    assert report.max_output_tokens == 42
    assert report.host
    assert report.commit == "abc1234"
    assert report.branch == "m7-first-brain"
    assert report.generated_at
    assert report.peak_rss_bytes > 0


def test_run_triage_bench_defaults_commit_and_branch_to_none() -> None:
    fixtures = [_fixture("t1", TriageClass.TRIVIAL)]
    triage = _ScriptedTriage(replies=[TriageClass.TRIVIAL])

    report = run_triage_bench(
        triage,
        fixtures,
        model_repo="m",
        prompt_variant="strict",
        max_output_tokens=50,
    )

    assert report.commit is None
    assert report.branch is None


def test_run_triage_bench_defaults_chat_template_to_false() -> None:
    fixtures = [_fixture("t1", TriageClass.TRIVIAL)]
    triage = _ScriptedTriage(replies=[TriageClass.TRIVIAL])

    report = run_triage_bench(
        triage,
        fixtures,
        model_repo="m",
        prompt_variant="strict",
        max_output_tokens=50,
    )

    assert report.chat_template is False


def test_run_triage_bench_passes_the_request_text_through() -> None:
    fixtures = [
        TriageFixture(id="t1", expect=TriageClass.TRIVIAL, request="what is 2+2?")
    ]
    triage = _ScriptedTriage(replies=[TriageClass.TRIVIAL])

    run_triage_bench(
        triage,
        fixtures,
        model_repo="m",
        prompt_variant="strict",
        max_output_tokens=50,
    )

    assert triage.requests == ["what is 2+2?"]


def test_triage_fixture_result_properties() -> None:
    correct = TriageFixtureResult(
        fixture_id="a",
        expected=TriageClass.TRIVIAL,
        actual=TriageClass.TRIVIAL,
        raw_output="trivial",
        latency_seconds=0.1,
        prompt_tokens=10,
        tags=frozenset(),
    )
    fallback = TriageFixtureResult(
        fixture_id="b",
        expected=TriageClass.COMPLEX,
        actual=None,
        raw_output="???",
        latency_seconds=0.1,
        prompt_tokens=10,
        tags=frozenset(),
    )

    assert correct.correct is True
    assert correct.parse_fallback is False
    assert correct.routed_to_brain is False
    assert fallback.correct is False
    assert fallback.parse_fallback is True
    assert fallback.routed_to_brain is True
