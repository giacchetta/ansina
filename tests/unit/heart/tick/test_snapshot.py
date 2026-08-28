from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import pytest

from ansina.heart.tick.snapshot import (
    SnapshotItem,
    StateSnapshotSource,
    build_prompt,
    collect_items,
)

_COUNT_CHARS: Callable[[str], int] = len


class _FakeSource:
    def __init__(self, name: str, items: Sequence[SnapshotItem]) -> None:
        self.name = name
        self._items = items

    def collect(self) -> Sequence[SnapshotItem]:
        return self._items


class _RaisingSource:
    name = "broken"

    def collect(self) -> Sequence[SnapshotItem]:
        raise RuntimeError("boom")


def test_collect_items_orders_highest_priority_first() -> None:
    low = SnapshotItem(source="a", text="low", priority=0)
    high = SnapshotItem(source="b", text="high", priority=5)
    sources: list[StateSnapshotSource] = [
        _FakeSource("a", [low]),
        _FakeSource("b", [high]),
    ]

    items = collect_items(sources)

    assert items == [high, low]


def test_collect_items_skips_a_raising_source(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    ok = SnapshotItem(source="ok", text="fine", priority=0)
    sources: list[StateSnapshotSource] = [_RaisingSource(), _FakeSource("ok", [ok])]

    items = collect_items(sources)

    assert items == [ok]
    assert any(line["level"] == "ERROR" for line in captured_logs())


def test_collect_items_empty_when_no_sources() -> None:
    assert collect_items([]) == []


def test_build_prompt_with_no_items_renders_no_pending_state() -> None:
    prompt = build_prompt([], budget_tokens=1000, token_count=_COUNT_CHARS)

    assert "(no pending state)" in prompt.text
    assert prompt.items_included == 0
    assert prompt.items_dropped == 0
    assert prompt.tokens == _COUNT_CHARS(prompt.text)


def test_build_prompt_includes_items_that_fit() -> None:
    items = [
        SnapshotItem(source="a", text="alpha", priority=1),
        SnapshotItem(source="b", text="beta", priority=0),
    ]

    prompt = build_prompt(items, budget_tokens=1000, token_count=_COUNT_CHARS)

    assert "alpha" in prompt.text
    assert "beta" in prompt.text
    assert prompt.items_included == 2
    assert prompt.items_dropped == 0


def _empty_prompt_tokens() -> int:
    """The fixed rendering overhead (template + "no pending state" filler) that
    `build_prompt` always pays regardless of `budget_tokens` — items are trimmed
    against `budget_tokens - this`, not against `budget_tokens` directly.
    """
    return build_prompt([], budget_tokens=10_000, token_count=_COUNT_CHARS).tokens


def test_build_prompt_drops_lowest_priority_items_over_budget() -> None:
    high = SnapshotItem(source="a", text="x" * 5, priority=2)
    low = SnapshotItem(source="b", text="y" * 5, priority=1)
    # Exactly enough headroom for `high` alone, none left for `low`.
    budget = _empty_prompt_tokens() + len(high.text)

    prompt = build_prompt([high, low], budget_tokens=budget, token_count=_COUNT_CHARS)

    assert "x" * 5 in prompt.text
    assert "y" * 5 not in prompt.text
    assert prompt.items_included == 1
    assert prompt.items_dropped == 1


def test_build_prompt_never_exceeds_the_token_budget() -> None:
    items = [SnapshotItem(source=f"s{i}", text="z" * 50, priority=i) for i in range(20)]
    budget = _empty_prompt_tokens() + 120  # room for ~2 of the 20 items

    prompt = build_prompt(items, budget_tokens=budget, token_count=_COUNT_CHARS)

    assert prompt.tokens <= budget
    assert prompt.items_dropped > 0


def test_build_prompt_logs_a_warning_when_items_are_dropped(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    items = [SnapshotItem(source=f"s{i}", text="z" * 50, priority=i) for i in range(20)]
    budget = _empty_prompt_tokens() + 120

    build_prompt(items, budget_tokens=budget, token_count=_COUNT_CHARS)

    assert any(line["level"] == "WARNING" for line in captured_logs())


def test_build_prompt_zero_budget_drops_every_item() -> None:
    items = [SnapshotItem(source="a", text="x", priority=0)]

    prompt = build_prompt(items, budget_tokens=0, token_count=_COUNT_CHARS)

    assert prompt.items_included == 0
    assert prompt.items_dropped == 1
    assert "(no pending state)" in prompt.text


@pytest.mark.parametrize("budget_tokens", [-100, 0])
def test_build_prompt_never_raises_on_a_non_positive_budget(budget_tokens: int) -> None:
    prompt = build_prompt(
        [SnapshotItem(source="a", text="x", priority=0)],
        budget_tokens=budget_tokens,
        token_count=_COUNT_CHARS,
    )

    assert prompt.items_included == 0
