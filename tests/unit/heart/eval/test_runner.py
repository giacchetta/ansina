from __future__ import annotations

from collections.abc import Sequence

from ansina.heart.eval.fixtures import TickFixture
from ansina.heart.eval.runner import FixtureResult, run_bench
from ansina.heart.runtime import BaseHeartRuntime
from ansina.heart.tick.decision import TickDecision
from ansina.heart.tick.snapshot import SnapshotItem


class _FakeHeart(BaseHeartRuntime):
    """Mirrors `tests/unit/heart/tick/test_loop.py`'s `_FakeHeart`: 1 token per
    character, already loaded, with a scripted reply per call (falling back to the
    last one once exhausted) or a fixed reply for every call.
    """

    def __init__(
        self,
        *,
        context_tokens: int = 1000,
        max_output_tokens: int = 50,
        reply: str | None = "idle",
        replies: Sequence[str] | None = None,
    ) -> None:
        super().__init__(
            context_tokens=context_tokens, max_output_tokens=max_output_tokens
        )
        self.load()
        self._reply = reply
        self._replies = list(replies) if replies is not None else None
        self.generate_calls = 0

    def _load_backend(self) -> None:
        pass

    def _generate(self, prompt: str, max_tokens: int) -> str:
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


def _fixture(
    fixture_id: str, expect: TickDecision, *, tags: frozenset[str] = frozenset()
) -> TickFixture:
    return TickFixture(
        id=fixture_id,
        expect=expect,
        items=(SnapshotItem(source="s", text="state"),),
        tags=tags,
    )


def test_run_bench_scores_every_fixture_correct_when_the_model_always_matches() -> None:
    fixtures = [
        _fixture("i1", TickDecision.IDLE),
        _fixture("a1", TickDecision.ACT),
        _fixture("e1", TickDecision.ESCALATE),
    ]
    heart = _FakeHeart(replies=["idle", "act", "escalate"])

    report = run_bench(
        heart,
        fixtures,
        budget_tokens=1000,
        max_output_tokens=50,
        model_repo="fake/model",
    )

    assert report.accuracy == 1.0
    assert report.parse_fallback_rate == 0.0
    assert report.false_act_or_escalate_on_obvious_idle == 0
    assert report.fixture_count == 3
    for decision in TickDecision:
        assert report.recall_by_class[decision] == 1.0
        assert report.class_counts[decision] == 1


def test_run_bench_counts_a_parse_fallback_separately_from_correct_idle() -> None:
    fixtures = [_fixture("i1", TickDecision.IDLE)]
    heart = _FakeHeart(reply="banana pancakes")

    report = run_bench(
        heart,
        fixtures,
        budget_tokens=1000,
        max_output_tokens=50,
        model_repo="fake/model",
    )

    assert report.accuracy == 0.0
    assert report.parse_fallback_rate == 1.0
    assert report.results[0].actual is None
    assert report.results[0].parse_fallback is True
    assert report.results[0].correct is False


def test_run_bench_flags_false_act_on_an_obviously_idle_fixture() -> None:
    fixtures = [
        _fixture("i1", TickDecision.IDLE, tags=frozenset({"obviously_idle"})),
    ]
    heart = _FakeHeart(reply="act")

    report = run_bench(
        heart,
        fixtures,
        budget_tokens=1000,
        max_output_tokens=50,
        model_repo="fake/model",
    )

    assert report.false_act_or_escalate_on_obvious_idle == 1


def test_run_bench_does_not_flag_a_wrong_idle_call_as_false_act_or_escalate() -> None:
    """A model that answers `idle` when `act` was expected is simply wrong — the
    obviously-idle gate only cares about a false *positive* (act/escalate) on a
    fixture that should have been idle, never a false negative.
    """
    fixtures = [_fixture("a1", TickDecision.ACT, tags=frozenset({"obviously_idle"}))]
    heart = _FakeHeart(reply="idle")

    report = run_bench(
        heart,
        fixtures,
        budget_tokens=1000,
        max_output_tokens=50,
        model_repo="fake/model",
    )

    assert report.false_act_or_escalate_on_obvious_idle == 0


def test_run_bench_renders_through_the_real_build_prompt() -> None:
    fixtures = [_fixture("i1", TickDecision.IDLE)]
    heart = _FakeHeart(reply="idle")

    report = run_bench(
        heart,
        fixtures,
        budget_tokens=1000,
        max_output_tokens=50,
        model_repo="fake/model",
    )

    assert report.results[0].prompt_tokens > 0
    assert report.prompt_tokens_min == report.prompt_tokens_max


def test_run_bench_uses_the_injected_perf_counter_for_latency() -> None:
    fixtures = [_fixture("i1", TickDecision.IDLE), _fixture("i2", TickDecision.IDLE)]
    heart = _FakeHeart(reply="idle")
    calls = iter([0.0, 1.0, 1.0, 3.0])

    report = run_bench(
        heart,
        fixtures,
        budget_tokens=1000,
        max_output_tokens=50,
        model_repo="fake/model",
        perf_counter=lambda: next(calls),
    )

    assert [r.latency_seconds for r in report.results] == [1.0, 2.0]
    assert report.latency_p50_seconds in (1.0, 2.0)
    assert report.latency_p95_seconds == 2.0


def test_run_bench_records_metadata() -> None:
    fixtures = [_fixture("i1", TickDecision.IDLE)]
    heart = _FakeHeart(reply="idle")

    report = run_bench(
        heart,
        fixtures,
        budget_tokens=1000,
        max_output_tokens=42,
        model_repo="fake/model",
        prompt_variant="strict",
        chat_template=True,
    )

    assert report.model_repo == "fake/model"
    assert report.prompt_variant == "strict"
    assert report.chat_template is True
    assert report.max_output_tokens == 42
    assert report.host
    assert report.generated_at
    assert report.peak_rss_bytes > 0


def test_run_bench_defaults_chat_template_to_false() -> None:
    fixtures = [_fixture("i1", TickDecision.IDLE)]
    heart = _FakeHeart(reply="idle")

    report = run_bench(
        heart, fixtures, budget_tokens=1000, max_output_tokens=50, model_repo="m"
    )

    assert report.chat_template is False


def test_fixture_result_correct_and_parse_fallback_properties() -> None:
    correct = FixtureResult(
        fixture_id="a",
        expected=TickDecision.IDLE,
        actual=TickDecision.IDLE,
        raw_output="idle",
        latency_seconds=0.1,
        prompt_tokens=10,
        tags=frozenset(),
    )
    wrong = FixtureResult(
        fixture_id="b",
        expected=TickDecision.IDLE,
        actual=TickDecision.ACT,
        raw_output="act",
        latency_seconds=0.1,
        prompt_tokens=10,
        tags=frozenset(),
    )
    fallback = FixtureResult(
        fixture_id="c",
        expected=TickDecision.IDLE,
        actual=None,
        raw_output="???",
        latency_seconds=0.1,
        prompt_tokens=10,
        tags=frozenset(),
    )

    assert correct.correct is True
    assert correct.parse_fallback is False
    assert wrong.correct is False
    assert wrong.parse_fallback is False
    assert wrong.false_act_or_escalate is True
    assert fallback.correct is False
    assert fallback.parse_fallback is True
    assert fallback.false_act_or_escalate is False
