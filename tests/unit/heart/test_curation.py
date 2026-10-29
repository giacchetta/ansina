from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from ansina.heart.curation import (
    CURATION_PROMPT_VARIANTS,
    DEFAULT_CURATION_TEMPLATE,
    DEFAULT_CURATION_VARIANT,
    ContextCurator,
    CurationEvent,
    CurationOutcome,
    HeartContextCurator,
    parse_curation,
    recency_window_selection,
    render_event,
    try_parse_curation,
)
from ansina.heart.runtime import BaseHeartRuntime


class _FakeCurationHeart(BaseHeartRuntime):
    """Mirrors `test_triage.py`'s own `_FakeTriageHeart`: 1 token per character,
    already loaded, with a scripted reply per call (falling back to the last one
    once exhausted) or a fixed reply for every call.
    """

    def __init__(
        self,
        *,
        context_tokens: int = 1000,
        max_output_tokens: int = 50,
        reply: str | None = "0",
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


class _ZeroTokenHeart(_FakeCurationHeart):
    """A degenerate fake whose tokenizer reports zero tokens for everything —
    exercises `HeartContextCurator.curate`'s `average_tokens > 0` guard.
    """

    def _token_count(self, text: str) -> int:
        return 0


def _event(sequence: int, *, raw_type: str = "t", payload: str = "p") -> CurationEvent:
    return CurationEvent(sequence=sequence, raw_type=raw_type, payload=payload)


# --- render_event --------------------------------------------------------------


def test_render_event_includes_sequence_type_and_payload() -> None:
    rendered = render_event(_event(3, raw_type="run_event", payload="hello"))
    assert rendered == "[3] run_event: hello"


# --- recency_window_selection ----------------------------------------------------


def test_recency_window_selection_keeps_the_most_recent_events_first() -> None:
    events = [_event(0), _event(1), _event(2)]
    cost = len(render_event(events[0]))

    selection = recency_window_selection(
        events, budget_tokens=cost * 2, token_count=len
    )

    assert selection == frozenset({1, 2})


def test_recency_window_selection_stops_at_the_first_event_that_would_overflow() -> (
    None
):
    events = [_event(0), _event(1), _event(2)]
    cost = len(render_event(events[0]))

    # Enough room for exactly two events, with one token to spare — never enough for
    # a third, since every rendered event here costs the same.
    selection = recency_window_selection(
        events, budget_tokens=cost * 2 + 1, token_count=len
    )

    assert selection == frozenset({1, 2})


def test_recency_window_selection_returns_empty_when_the_budget_is_zero() -> None:
    events = [_event(0), _event(1)]

    selection = recency_window_selection(events, budget_tokens=0, token_count=len)

    assert selection == frozenset()


def test_recency_window_selection_keeps_everything_when_the_budget_is_generous() -> (
    None
):
    events = [_event(0), _event(1), _event(2)]
    total = sum(len(render_event(e)) for e in events)

    selection = recency_window_selection(events, budget_tokens=total, token_count=len)

    assert selection == frozenset({0, 1, 2})


# --- try_parse_curation / parse_curation ------------------------------------------


def test_try_parse_curation_extracts_integers_in_first_mentioned_order() -> None:
    result = try_parse_curation(
        "Keep: 2, 5, 9", valid_sequences=frozenset({2, 5, 9, 14})
    )
    assert result == (2, 5, 9)


def test_try_parse_curation_filters_out_of_range_values() -> None:
    result = try_parse_curation("2, 99, 5", valid_sequences=frozenset({2, 5}))
    assert result == (2, 5)


def test_try_parse_curation_deduplicates_keeping_first_occurrence() -> None:
    result = try_parse_curation("5, 2, 5", valid_sequences=frozenset({2, 5}))
    assert result == (5, 2)


@pytest.mark.parametrize("raw", ["", "   ", "no numbers here", "42 and 99"])
def test_try_parse_curation_returns_none_when_nothing_valid_is_found(
    raw: str,
) -> None:
    assert try_parse_curation(raw, valid_sequences=frozenset({0, 1})) is None


def test_try_parse_curation_never_logs(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    try_parse_curation("nothing", valid_sequences=frozenset({0}))
    assert captured_logs() == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Thinking...\n</think>\n2, 5", (2, 5)),
        ("maybe 9?</think>2, 5", (2, 5)),
    ],
)
def test_try_parse_curation_uses_text_after_the_last_think_close_tag(
    raw: str, expected: tuple[int, ...]
) -> None:
    assert try_parse_curation(raw, valid_sequences=frozenset({2, 5, 9})) == expected


def test_try_parse_curation_is_unaffected_when_no_think_tag_is_present() -> None:
    assert try_parse_curation("2, 5", valid_sequences=frozenset({2, 5})) == (2, 5)


def test_parse_curation_returns_the_parsed_set_when_parseable() -> None:
    result = parse_curation(
        "2, 5", valid_sequences=frozenset({2, 5}), fallback=frozenset({9})
    )
    assert result == frozenset({2, 5})


def test_parse_curation_defaults_to_the_fallback_when_unparseable() -> None:
    result = parse_curation(
        "no idea", valid_sequences=frozenset({2, 5}), fallback=frozenset({9})
    )
    assert result == frozenset({9})


def test_parse_curation_logs_a_warning_when_unparseable(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    parse_curation("no idea", valid_sequences=frozenset({2}), fallback=frozenset())
    lines = captured_logs()
    assert any(line["level"] == "WARNING" for line in lines)


# --- CurationOutcome ---------------------------------------------------------------


def test_curation_outcome_parse_fallback_is_true_only_when_parsed_is_none() -> None:
    parsed = CurationOutcome(
        events=(),
        selected=frozenset({0}),
        parsed=frozenset({0}),
        raw_output="0",
        prompt_tokens=1,
        exceeded_budget=False,
    )
    fallback = CurationOutcome(
        events=(),
        selected=frozenset(),
        parsed=None,
        raw_output="???",
        prompt_tokens=1,
        exceeded_budget=False,
    )
    assert parsed.parse_fallback is False
    assert fallback.parse_fallback is True


# --- CURATION_PROMPT_VARIANTS -------------------------------------------------------


def test_default_curation_variant_is_strict() -> None:
    assert DEFAULT_CURATION_VARIANT == "strict"
    assert DEFAULT_CURATION_TEMPLATE is CURATION_PROMPT_VARIANTS["strict"]


def test_every_curation_variant_has_both_required_placeholders() -> None:
    for name, template in CURATION_PROMPT_VARIANTS.items():
        rendered = template.format(events="EVENTLIST", max_events=3)
        assert "{events}" not in rendered, f"{name} left an unfilled events placeholder"
        assert "{max_events}" not in rendered, (
            f"{name} left an unfilled max_events placeholder"
        )
        assert "EVENTLIST" in rendered, f"{name} never substituted events"
        assert "3" in rendered, f"{name} never substituted max_events"


def test_curation_variants_are_distinct() -> None:
    templates = list(CURATION_PROMPT_VARIANTS.values())
    assert len(templates) == len(set(templates))


# --- HeartContextCurator -----------------------------------------------------------


def test_heart_context_curator_satisfies_the_context_curator_protocol() -> None:
    heart = _FakeCurationHeart(reply="0")
    curator: ContextCurator = HeartContextCurator(heart)
    assert isinstance(curator, ContextCurator)


def test_curate_returns_the_parsed_selection_when_it_fits_the_budget() -> None:
    events = (_event(0), _event(1), _event(2))
    heart = _FakeCurationHeart(reply="0, 2")
    curator = HeartContextCurator(heart)
    full_budget = sum(heart.token_count(render_event(e)) for e in events)

    outcome = curator.curate(events, budget_tokens=full_budget)

    assert outcome.parsed == frozenset({0, 2})
    assert outcome.selected == frozenset({0, 2})
    assert outcome.exceeded_budget is False
    assert outcome.parse_fallback is False
    assert outcome.raw_output == "0, 2"
    assert outcome.events == events


def test_curate_trims_a_selection_that_does_not_fit_the_budget() -> None:
    events = (_event(0), _event(1), _event(2))
    heart = _FakeCurationHeart(reply="0, 1, 2")
    curator = HeartContextCurator(heart)
    cost_each = len(render_event(events[0]))

    outcome = curator.curate(events, budget_tokens=cost_each * 2)

    # Only the first two of the model's own listed order fit — the third (sequence
    # 2) is dropped, not reordered.
    assert outcome.selected == frozenset({0, 1})
    assert outcome.parsed == frozenset({0, 1, 2})
    assert outcome.exceeded_budget is True


def test_curate_falls_back_to_the_recency_baseline_on_an_unparseable_reply(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    events = (_event(0), _event(1), _event(2))
    heart = _FakeCurationHeart(reply="no idea")
    curator = HeartContextCurator(heart)
    cost_each = len(render_event(events[0]))
    budget = cost_each * 2

    outcome = curator.curate(events, budget_tokens=budget)

    assert outcome.parsed is None
    assert outcome.parse_fallback is True
    assert outcome.exceeded_budget is False
    assert outcome.selected == recency_window_selection(
        events, budget_tokens=budget, token_count=heart.token_count
    )
    assert any(line["level"] == "WARNING" for line in captured_logs())


def test_curate_prompt_tokens_matches_the_rendered_prompt_length() -> None:
    events = (_event(0),)
    heart = _FakeCurationHeart(reply="0")
    curator = HeartContextCurator(heart)

    outcome = curator.curate(events, budget_tokens=100)

    assert heart.last_prompt is not None
    assert outcome.prompt_tokens == heart.token_count(heart.last_prompt)


def test_curate_states_an_approximate_event_budget_in_the_prompt() -> None:
    events = (_event(0), _event(1))
    heart = _FakeCurationHeart(reply="0")
    curator = HeartContextCurator(heart)
    cost_each = len(render_event(events[0]))

    curator.curate(events, budget_tokens=cost_each)

    assert heart.last_prompt is not None
    # A budget that fits exactly one event's own cost should state "1" as the
    # approximate event-count target — checked against the specific phrase both
    # templates render it into, since the event transcript itself also contains
    # bare digits (`[0]`, `[1]`) that a looser substring check would collide with.
    assert "at most 1 event" in heart.last_prompt


def test_curate_falls_back_to_an_event_count_target_when_average_tokens_is_zero() -> (
    None
):
    events = (_event(0), _event(1))
    heart = _ZeroTokenHeart(reply="no idea")
    curator = HeartContextCurator(heart)

    outcome = curator.curate(events, budget_tokens=10)

    assert heart.last_prompt is not None
    assert f"at most {len(events)} event" in heart.last_prompt
    # An unparseable reply still resolves to a valid selection — every rendered cost
    # is 0 here, so the recency baseline keeps both events regardless of budget.
    assert outcome.parse_fallback is True
    assert outcome.selected == frozenset({0, 1})


def test_curate_handles_zero_events() -> None:
    heart = _FakeCurationHeart(reply="")
    curator = HeartContextCurator(heart)

    outcome = curator.curate((), budget_tokens=10)

    assert outcome.events == ()
    assert outcome.selected == frozenset()
    assert outcome.parse_fallback is True
