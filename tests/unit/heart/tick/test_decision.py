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
