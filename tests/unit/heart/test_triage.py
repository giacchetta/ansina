from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from ansina.heart.runtime import BaseHeartRuntime
from ansina.heart.triage import (
    DEFAULT_TRIAGE_TEMPLATE,
    DEFAULT_TRIAGE_VARIANT,
    TRIAGE_PROMPT_VARIANTS,
    HeartRequestTriage,
    RequestTriage,
    TriageClass,
    TriageOutcome,
    parse_triage,
    routes_to_brain,
    try_parse_triage,
)


class _FakeTriageHeart(BaseHeartRuntime):
    """Mirrors `tests/unit/heart/eval/test_runner.py`'s `_FakeHeart`: 1 token per
    character, already loaded, with a scripted reply per call (falling back to the
    last one once exhausted) or a fixed reply for every call.
    """

    def __init__(
        self,
        *,
        context_tokens: int = 1000,
        max_output_tokens: int = 50,
        reply: str | None = "trivial",
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


# --- TriageClass / routes_to_brain -----------------------------------------------


def test_triage_class_values_are_stable() -> None:
    assert TriageClass.TRIVIAL.value == "trivial"
    assert TriageClass.TOOL_ONLY.value == "tool-only"
    assert TriageClass.COMPLEX.value == "complex"


@pytest.mark.parametrize(
    ("triage_class", "expected"),
    [
        (TriageClass.TRIVIAL, False),
        (TriageClass.TOOL_ONLY, True),
        (TriageClass.COMPLEX, True),
    ],
)
def test_routes_to_brain_is_2_way(triage_class: TriageClass, expected: bool) -> None:
    """Today's routing: only `trivial` answers without the Brain — `tool-only`
    escalates exactly like `complex` does, since no `ToolRegistry` exists yet.
    """
    assert routes_to_brain(triage_class) is expected


# --- try_parse_triage / parse_triage ----------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("trivial", TriageClass.TRIVIAL),
        ("tool-only", TriageClass.TOOL_ONLY),
        ("tool_only", TriageClass.TOOL_ONLY),
        ("toolonly", TriageClass.TOOL_ONLY),
        ("complex", TriageClass.COMPLEX),
        ("TRIVIAL", TriageClass.TRIVIAL),
        ("  complex  ", TriageClass.COMPLEX),
        ("Tool-Only.", TriageClass.TOOL_ONLY),
        ("Complex, this needs judgment.", TriageClass.COMPLEX),
        ('"trivial"', TriageClass.TRIVIAL),
    ],
)
def test_try_parse_triage_recognizes_the_first_word(
    raw: str, expected: TriageClass
) -> None:
    assert try_parse_triage(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "unsure", "I don't know"])
def test_try_parse_triage_returns_none_when_unparseable(raw: str) -> None:
    assert try_parse_triage(raw) is None


def test_try_parse_triage_never_logs(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    try_parse_triage("bananas")
    assert captured_logs() == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Thinking...\n</think>\ntrivial", TriageClass.TRIVIAL),
        ("maybe tool-only?</think>complex", TriageClass.COMPLEX),
    ],
)
def test_try_parse_triage_uses_the_text_after_the_last_think_close_tag(
    raw: str, expected: TriageClass
) -> None:
    assert try_parse_triage(raw) == expected


def test_try_parse_triage_is_unaffected_when_no_think_tag_is_present() -> None:
    assert try_parse_triage("complex") == TriageClass.COMPLEX


def test_parse_triage_delegates_to_try_parse_triage() -> None:
    assert parse_triage("tool-only") == try_parse_triage("tool-only")


@pytest.mark.parametrize("raw", ["", "unsure", "I don't know"])
def test_parse_triage_defaults_to_complex_when_unparseable(raw: str) -> None:
    """The safe default for triage is `complex` (always escalate) — the opposite
    direction from the tick loop's own `idle` default, since the two safe choices are
    different for the two duties.
    """
    assert parse_triage(raw) is TriageClass.COMPLEX


def test_parse_triage_logs_a_warning_when_unparseable(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    parse_triage("bananas")
    lines = captured_logs()
    assert any(line["level"] == "WARNING" for line in lines)


# --- TriageOutcome -----------------------------------------------------------------


def test_triage_outcome_parse_fallback_is_true_only_when_parsed_is_none() -> None:
    parsed = TriageOutcome(
        request="r",
        triage_class=TriageClass.TRIVIAL,
        parsed=TriageClass.TRIVIAL,
        raw_output="trivial",
        prompt_tokens=10,
    )
    fallback = TriageOutcome(
        request="r",
        triage_class=TriageClass.COMPLEX,
        parsed=None,
        raw_output="???",
        prompt_tokens=10,
    )
    assert parsed.parse_fallback is False
    assert fallback.parse_fallback is True


def test_triage_outcome_routes_to_brain_matches_the_module_function() -> None:
    outcome = TriageOutcome(
        request="r",
        triage_class=TriageClass.TOOL_ONLY,
        parsed=TriageClass.TOOL_ONLY,
        raw_output="tool-only",
        prompt_tokens=10,
    )
    assert outcome.routes_to_brain is True


# --- TRIAGE_PROMPT_VARIANTS ---------------------------------------------------------


def test_default_triage_variant_is_strict() -> None:
    assert DEFAULT_TRIAGE_VARIANT == "strict"
    assert DEFAULT_TRIAGE_TEMPLATE is TRIAGE_PROMPT_VARIANTS["strict"]


def test_every_triage_variant_has_exactly_one_request_placeholder() -> None:
    for name, template in TRIAGE_PROMPT_VARIANTS.items():
        rendered = template.format(request="x")
        assert "{request}" not in rendered, f"{name} left an unfilled placeholder"
        assert "x" in rendered, f"{name} never substituted request"


def test_every_triage_variant_names_all_three_classes() -> None:
    for name, template in TRIAGE_PROMPT_VARIANTS.items():
        rendered = template.format(request="x")
        for word in ("trivial", "tool-only", "complex"):
            assert word in rendered, f"{name} is missing {word!r}"


def test_triage_variants_are_distinct() -> None:
    templates = list(TRIAGE_PROMPT_VARIANTS.values())
    assert len(templates) == len(set(templates))


# --- HeartRequestTriage --------------------------------------------------------------


def test_heart_request_triage_satisfies_the_request_triage_protocol() -> None:
    heart = _FakeTriageHeart(reply="trivial")
    triage: RequestTriage = HeartRequestTriage(heart)
    assert isinstance(triage, RequestTriage)


def test_heart_request_triage_classify_returns_the_parsed_class() -> None:
    heart = _FakeTriageHeart(reply="complex")
    triage = HeartRequestTriage(heart)

    outcome = triage.classify("design a migration plan")

    assert outcome.request == "design a migration plan"
    assert outcome.triage_class is TriageClass.COMPLEX
    assert outcome.parsed is TriageClass.COMPLEX
    assert outcome.raw_output == "complex"
    assert outcome.parse_fallback is False
    assert outcome.prompt_tokens == heart.token_count(
        DEFAULT_TRIAGE_TEMPLATE.format(request="design a migration plan")
    )


def test_heart_request_triage_classify_defaults_to_complex_on_a_parse_fallback(
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    heart = _FakeTriageHeart(reply="not sure")
    triage = HeartRequestTriage(heart)

    outcome = triage.classify("what should we do?")

    assert outcome.parsed is None
    assert outcome.parse_fallback is True
    assert outcome.triage_class is TriageClass.COMPLEX
    assert any(line["level"] == "WARNING" for line in captured_logs())


def test_heart_request_triage_renders_the_request_into_the_template() -> None:
    heart = _FakeTriageHeart(reply="trivial")
    triage = HeartRequestTriage(heart, template="Q: {request}\nA:")

    triage.classify("what is 2+2?")

    assert heart.last_prompt == "Q: what is 2+2?\nA:"


def test_heart_request_triage_passes_max_output_tokens_through() -> None:
    heart = _FakeTriageHeart(reply="trivial", max_output_tokens=999)
    triage = HeartRequestTriage(heart, max_output_tokens=7)

    triage.classify("short")

    assert heart.last_max_tokens == 7


def test_heart_request_triage_defaults_max_output_tokens_to_the_runtimes_own() -> None:
    heart = _FakeTriageHeart(reply="trivial", max_output_tokens=42)
    triage = HeartRequestTriage(heart)

    triage.classify("short")

    assert heart.last_max_tokens == 42
