from __future__ import annotations

import math
from collections.abc import AsyncGenerator, Sequence

import pytest

from ansina.brain.events import (
    BrainDone,
    BrainErrorClass,
    BrainErrorEvent,
    BrainEvent,
    BrainTextDelta,
)
from ansina.brain.provider import BrainRequest
from ansina.heart.curation import (
    CurationEvent,
    CurationOutcome,
    recency_window_selection,
    render_event,
)
from ansina.heart.eval.curation_runner import (
    DEFAULT_BUDGET_FRACTION,
    CurationFixtureResult,
    _identifies_failure_cause,
    _render_selection,
    run_curation_bench,
)
from ansina.heart.eval.fixtures import CurationFixture


def _event(sequence: int, *, raw_type: str = "t", payload: str = "p") -> CurationEvent:
    return CurationEvent(sequence=sequence, raw_type=raw_type, payload=payload)


def _result(
    *, must_keep: frozenset[int], selected_curated: frozenset[int]
) -> CurationFixtureResult:
    return CurationFixtureResult(
        fixture_id="x",
        must_keep=must_keep,
        selected_curated=selected_curated,
        selected_truncated=frozenset(),
        parse_fallback=False,
        exceeded_budget=False,
        curated_answer="a",
        truncated_answer="b",
        curated_identified=False,
        truncated_identified=False,
        curation_latency_seconds=0.0,
        prompt_tokens=0,
        tags=frozenset(),
    )


def _fixture(
    fixture_id: str,
    events: Sequence[CurationEvent],
    *,
    must_keep: frozenset[int],
    failure_cause: str = "something failed",
    tags: frozenset[str] = frozenset(),
) -> CurationFixture:
    return CurationFixture(
        id=fixture_id,
        events=tuple(events),
        must_keep=must_keep,
        failure_cause=failure_cause,
        tags=tags,
    )


class _ScriptedCurator:
    """A `ContextCurator` fake: one scripted `parsed` selection per call (`None` for
    a parse fallback — scored via the real `recency_window_selection`, exactly the
    way `HeartContextCurator` itself would fall back), plus an independently scripted
    `exceeded_budget` flag, each falling back to its own last entry once exhausted.
    """

    def __init__(
        self,
        *,
        selections: Sequence[frozenset[int] | None],
        exceeded: Sequence[bool] | None = None,
    ) -> None:
        self._selections = list(selections)
        self._exceeded = (
            list(exceeded) if exceeded is not None else [False] * len(self._selections)
        )
        self.calls = 0

    def curate(
        self, events: Sequence[CurationEvent], *, budget_tokens: int
    ) -> CurationOutcome:
        index = min(self.calls, len(self._selections) - 1)
        exceeded_index = min(self.calls, len(self._exceeded) - 1)
        self.calls += 1
        parsed = self._selections[index]
        if parsed is None:
            selected = recency_window_selection(
                events, budget_tokens=budget_tokens, token_count=len
            )
        else:
            selected = parsed
        return CurationOutcome(
            events=tuple(events),
            selected=selected,
            parsed=parsed,
            raw_output=(",".join(str(s) for s in sorted(parsed)) if parsed else "???"),
            prompt_tokens=10,
            exceeded_budget=self._exceeded[exceeded_index],
        )


class _ScriptedBrain:
    """A `BrainProvider` fake: one scripted text answer per `stream()` call, in call
    order — `run_curation_bench` calls it twice per fixture (curated, then
    truncated), so answers are scripted in that interleaved order, falling back to
    the last one once exhausted.
    """

    def __init__(self, *, answers: Sequence[str]) -> None:
        self._answers = list(answers)
        self.calls = 0
        self.requests: list[BrainRequest] = []

    def stream(self, request: BrainRequest) -> AsyncGenerator[BrainEvent]:
        self.requests.append(request)
        index = min(self.calls, len(self._answers) - 1)
        self.calls += 1
        answer = self._answers[index]

        async def _gen() -> AsyncGenerator[BrainEvent]:
            yield BrainTextDelta(text=answer)
            yield BrainDone()

        return _gen()

    async def aclose(self) -> None:
        pass


class _ErroringBrain:
    """A `BrainProvider` fake whose every `stream()` call terminates with a
    `BrainErrorEvent` — exercises `_ask_brain`'s own error-rendering path.
    """

    def stream(self, request: BrainRequest) -> AsyncGenerator[BrainEvent]:
        async def _gen() -> AsyncGenerator[BrainEvent]:
            yield BrainErrorEvent(
                error_class=BrainErrorClass.INTERNAL, message="boom", retryable=False
            )

        return _gen()

    async def aclose(self) -> None:
        pass


# --- _identifies_failure_cause / _render_selection ---------------------------------


@pytest.mark.parametrize(
    ("answer", "cause", "expected"),
    [
        ("it failed due to exact phrase here", "exact phrase", True),
        ("IT FAILED DUE TO EXACT PHRASE HERE", "exact phrase", True),
        ("assumption... wrong, as it turned out", "wrong assumption", True),
        ("nothing relevant here at all", "wrong assumption", False),
        ("some text", "", False),
        ("some text", "a to it", False),
    ],
)
def test_identifies_failure_cause(answer: str, cause: str, expected: bool) -> None:
    assert _identifies_failure_cause(answer, cause) is expected


def test_render_selection_orders_chronologically_regardless_of_set_order() -> None:
    events = [
        _event(0, payload="first"),
        _event(1, payload="second"),
        _event(2, payload="third"),
    ]

    text = _render_selection(events, frozenset({2, 0}))

    assert text.splitlines() == [render_event(events[0]), render_event(events[2])]


# --- run_curation_bench -------------------------------------------------------------


async def test_run_curation_bench_computes_must_keep_recall_and_identification() -> (
    None
):
    events = [_event(0, payload="a"), _event(1, payload="b"), _event(2, payload="c")]
    fixture = _fixture(
        "c1", events, must_keep=frozenset({0}), failure_cause="wrong assumption"
    )
    curator = _ScriptedCurator(selections=[frozenset({0, 2})])
    brain = _ScriptedBrain(
        answers=[
            "It failed because of a wrong assumption early on.",
            "It failed at the final step for an unrelated reason.",
        ]
    )

    report = await run_curation_bench(
        curator,
        brain,
        [fixture],
        model_repo="fake/model",
        prompt_variant="strict",
        brain_model="fake-brain",
        max_output_tokens=50,
        token_count=len,
    )

    assert report.fixture_count == 1
    assert report.must_keep_recall_rate == 1.0
    assert report.curated_identified_count == 1
    assert report.truncated_identified_count == 0
    result = report.results[0]
    assert result.curated_recall is True
    assert result.truncated_recall is False
    assert result.curated_identified is True
    assert result.truncated_identified is False


async def test_run_curation_bench_flags_a_dropped_must_keep_event() -> None:
    events = [_event(0), _event(1)]
    fixture = _fixture("c1", events, must_keep=frozenset({0}))
    curator = _ScriptedCurator(selections=[frozenset({1})])
    brain = _ScriptedBrain(answers=["unrelated", "unrelated"])

    report = await run_curation_bench(
        curator,
        brain,
        [fixture],
        model_repo="m",
        prompt_variant="strict",
        brain_model="b",
        max_output_tokens=50,
        token_count=len,
    )

    assert report.results[0].curated_recall is False
    assert report.must_keep_recall_rate == 0.0


async def test_run_curation_bench_counts_a_parse_fallback() -> None:
    events = [_event(0), _event(1)]
    fixture = _fixture("c1", events, must_keep=frozenset({0}))
    curator = _ScriptedCurator(selections=[None])
    brain = _ScriptedBrain(answers=["x", "x"])

    report = await run_curation_bench(
        curator,
        brain,
        [fixture],
        model_repo="m",
        prompt_variant="strict",
        brain_model="b",
        max_output_tokens=100,
        token_count=len,
    )

    assert report.parse_fallback_rate == 1.0
    assert report.results[0].parse_fallback is True


async def test_run_curation_bench_counts_exceeded_budget() -> None:
    events = [_event(0), _event(1)]
    fixture = _fixture("c1", events, must_keep=frozenset({0}))
    curator = _ScriptedCurator(selections=[frozenset({0})], exceeded=[True])
    brain = _ScriptedBrain(answers=["x", "x"])

    report = await run_curation_bench(
        curator,
        brain,
        [fixture],
        model_repo="m",
        prompt_variant="strict",
        brain_model="b",
        max_output_tokens=50,
        token_count=len,
    )

    assert report.exceeded_budget_count == 1
    assert report.results[0].exceeded_budget is True


async def test_run_curation_bench_computes_truncated_selection_via_baseline() -> None:
    events = [
        _event(0, payload="aa"),
        _event(1, payload="bb"),
        _event(2, payload="cc"),
    ]
    fixture = _fixture("c1", events, must_keep=frozenset({0}))
    curator = _ScriptedCurator(selections=[frozenset({0})])
    brain = _ScriptedBrain(answers=["x", "x"])

    report = await run_curation_bench(
        curator,
        brain,
        [fixture],
        model_repo="m",
        prompt_variant="strict",
        brain_model="b",
        max_output_tokens=50,
        budget_fraction=0.5,
        token_count=len,
    )

    full_tokens = sum(len(render_event(e)) for e in events)
    budget_tokens = max(1, math.floor(0.5 * full_tokens))
    expected_truncated = recency_window_selection(
        events, budget_tokens=budget_tokens, token_count=len
    )
    assert report.results[0].selected_truncated == expected_truncated


async def test_run_curation_bench_sends_the_chronological_transcript_to_the_brain() -> (
    None
):
    events = [_event(0, payload="first"), _event(1, payload="second")]
    fixture = _fixture("c1", events, must_keep=frozenset({0}))
    curator = _ScriptedCurator(selections=[frozenset({0, 1})])
    brain = _ScriptedBrain(answers=["x", "x"])

    await run_curation_bench(
        curator,
        brain,
        [fixture],
        model_repo="m",
        prompt_variant="strict",
        brain_model="b",
        max_output_tokens=50,
        token_count=len,
    )

    assert len(brain.requests) == 2
    curated_request, truncated_request = brain.requests
    assert "[0] t: first" in curated_request.messages[0].content
    assert "[1] t: second" in curated_request.messages[0].content
    assert curated_request.model == "b"
    assert curated_request.max_output_tokens == 50
    assert truncated_request.model == "b"


async def test_run_curation_bench_renders_a_brain_error_into_the_answer_text() -> None:
    events = [_event(0)]
    fixture = _fixture("c1", events, must_keep=frozenset({0}))
    curator = _ScriptedCurator(selections=[frozenset({0})])
    brain = _ErroringBrain()

    report = await run_curation_bench(
        curator,
        brain,
        [fixture],
        model_repo="m",
        prompt_variant="strict",
        brain_model="b",
        max_output_tokens=50,
        token_count=len,
    )

    assert "brain error" in report.results[0].curated_answer
    assert "boom" in report.results[0].curated_answer
    assert report.results[0].curated_identified is False


async def test_run_curation_bench_records_metadata() -> None:
    events = [_event(0)]
    fixture = _fixture("c1", events, must_keep=frozenset({0}))
    curator = _ScriptedCurator(selections=[frozenset({0})])
    brain = _ScriptedBrain(answers=["x", "x"])

    report = await run_curation_bench(
        curator,
        brain,
        [fixture],
        model_repo="fake/model",
        prompt_variant="strict",
        brain_model="fake-brain",
        max_output_tokens=42,
        budget_fraction=0.75,
        token_count=len,
        chat_template=True,
        commit="abc1234",
        branch="m9-opencode-executor-hands",
    )

    assert report.model_repo == "fake/model"
    assert report.prompt_variant == "strict"
    assert report.brain_model == "fake-brain"
    assert report.budget_fraction == 0.75
    assert report.max_output_tokens == 42
    assert report.chat_template is True
    assert report.commit == "abc1234"
    assert report.branch == "m9-opencode-executor-hands"
    assert report.host
    assert report.generated_at
    assert report.peak_rss_bytes > 0


async def test_run_curation_bench_defaults_commit_branch_and_chat_template() -> None:
    events = [_event(0)]
    fixture = _fixture("c1", events, must_keep=frozenset({0}))
    curator = _ScriptedCurator(selections=[frozenset({0})])
    brain = _ScriptedBrain(answers=["x", "x"])

    report = await run_curation_bench(
        curator,
        brain,
        [fixture],
        model_repo="m",
        prompt_variant="strict",
        brain_model="b",
        max_output_tokens=50,
        token_count=len,
    )

    assert report.commit is None
    assert report.branch is None
    assert report.chat_template is False
    assert report.budget_fraction == DEFAULT_BUDGET_FRACTION


async def test_run_curation_bench_uses_the_injected_perf_counter_for_latency() -> None:
    events = [_event(0)]
    fixture_one = _fixture("c1", events, must_keep=frozenset({0}))
    fixture_two = _fixture("c2", events, must_keep=frozenset({0}))
    curator = _ScriptedCurator(selections=[frozenset({0}), frozenset({0})])
    brain = _ScriptedBrain(answers=["x"] * 4)
    calls = iter([0.0, 1.0, 1.0, 3.0])

    report = await run_curation_bench(
        curator,
        brain,
        [fixture_one, fixture_two],
        model_repo="m",
        prompt_variant="strict",
        brain_model="b",
        max_output_tokens=50,
        token_count=len,
        perf_counter=lambda: next(calls),
    )

    assert [r.curation_latency_seconds for r in report.results] == [1.0, 2.0]
    assert report.latency_p95_seconds == 2.0


# --- CurationFixtureResult ----------------------------------------------------------


def test_curation_fixture_result_recall_properties() -> None:
    result_recall = _result(
        must_keep=frozenset({0}), selected_curated=frozenset({0, 1})
    )
    result_no_recall = _result(
        must_keep=frozenset({0}), selected_curated=frozenset({1})
    )

    assert result_recall.curated_recall is True
    assert result_no_recall.curated_recall is False


def test_curation_fixture_result_truncated_recall_property() -> None:
    result = CurationFixtureResult(
        fixture_id="x",
        must_keep=frozenset({0}),
        selected_curated=frozenset({0}),
        selected_truncated=frozenset({1}),
        parse_fallback=False,
        exceeded_budget=False,
        curated_answer="a",
        truncated_answer="b",
        curated_identified=False,
        truncated_identified=False,
        curation_latency_seconds=0.0,
        prompt_tokens=0,
        tags=frozenset(),
    )

    assert result.truncated_recall is False
