from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from ansina.heart.tick.decision import TickDecision, parse_decision, try_parse_decision


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("idle", TickDecision.IDLE),
        ("act", TickDecision.ACT),
        ("escalate", TickDecision.ESCALATE),
        ("IDLE", TickDecision.IDLE),
        ("  act  ", TickDecision.ACT),
        ("escalate.", TickDecision.ESCALATE),
        ("Escalate, please hand this off.", TickDecision.ESCALATE),
        ('"act"', TickDecision.ACT),
    ],
)
def test_parse_decision_recognizes_the_first_word(
    raw: str, expected: TickDecision
) -> None:
    assert parse_decision(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "sleeping", "I'm not sure what to do"])
def test_parse_decision_defaults_to_idle_when_unparseable(raw: str) -> None:
    assert parse_decision(raw) is TickDecision.IDLE


def test_parse_decision_logs_a_warning_when_unparseable(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    parse_decision("bananas")
    lines = captured_logs()
    assert any(line["level"] == "WARNING" for line in lines)


def test_tick_decision_values_are_stable() -> None:
    assert TickDecision.IDLE.value == "idle"
    assert TickDecision.ACT.value == "act"
    assert TickDecision.ESCALATE.value == "escalate"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("idle", TickDecision.IDLE),
        ("act", TickDecision.ACT),
        ("escalate", TickDecision.ESCALATE),
        ("Escalate, please hand this off.", TickDecision.ESCALATE),
    ],
)
def test_try_parse_decision_recognizes_the_first_word(
    raw: str, expected: TickDecision
) -> None:
    assert try_parse_decision(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "sleeping", "I'm not sure what to do"])
def test_try_parse_decision_returns_none_when_unparseable(raw: str) -> None:
    assert try_parse_decision(raw) is None


def test_try_parse_decision_never_logs(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    """Unlike `parse_decision`, this is a pure classifier — the bench needs to call it
    once per fixture without a warning line per unparseable reply.
    """
    try_parse_decision("bananas")
    assert captured_logs() == []


def test_parse_decision_delegates_to_try_parse_decision() -> None:
    assert parse_decision("act") == try_parse_decision("act")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Thinking Process:\n1. blah\n</think>\n\nidle", TickDecision.IDLE),
        ("some reasoning here</think>act", TickDecision.ACT),
        ("word1</think>word2</think>escalate", TickDecision.ESCALATE),
    ],
)
def test_try_parse_decision_uses_the_text_after_the_last_think_close_tag(
    raw: str, expected: TickDecision
) -> None:
    assert try_parse_decision(raw) == expected


def test_try_parse_decision_ignores_a_decision_word_mentioned_inside_the_thinking() -> (
    None
):
    """The exact issue #53 finding: a wandering chain-of-thought can mention one of
    the three real words ahead of the model's actual final answer — the word after
    `</think>` must win, not whichever one appears first in the whole reply.
    """
    raw = "I could say act here, but actually idle fits better.\n</think>\nidle"

    assert try_parse_decision(raw) == TickDecision.IDLE


def test_try_parse_decision_is_unaffected_when_no_think_tag_is_present() -> None:
    """A non-reasoning model's reply (or the pre-#53 raw-prompt path) never contains
    `</think>` — `rsplit` with no match must leave the string untouched.
    """
    assert try_parse_decision("act") == TickDecision.ACT
