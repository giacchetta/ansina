from __future__ import annotations

from ansina.heart.tick.prompts import (
    DEFAULT_PROMPT_VARIANT,
    DEFAULT_TEMPLATE,
    NO_PENDING_STATE,
    PROMPT_VARIANTS,
)


def test_default_variant_is_baseline() -> None:
    assert DEFAULT_PROMPT_VARIANT == "baseline"
    assert DEFAULT_TEMPLATE is PROMPT_VARIANTS["baseline"]


def test_every_variant_has_exactly_one_state_placeholder() -> None:
    for name, template in PROMPT_VARIANTS.items():
        rendered = template.format(state="x")
        assert "{state}" not in rendered, f"{name} left an unfilled placeholder"
        assert "x" in rendered, f"{name} never substituted state"


def test_every_variant_names_all_three_decisions() -> None:
    for name, template in PROMPT_VARIANTS.items():
        rendered = template.format(state=NO_PENDING_STATE)
        for word in ("idle", "act", "escalate"):
            assert word in rendered, f"{name} is missing {word!r}"


def test_variants_are_distinct() -> None:
    templates = list(PROMPT_VARIANTS.values())
    assert len(templates) == len(set(templates))


def test_baseline_matches_the_pre_issue_53_inline_template() -> None:
    """`build_prompt`'s default must stay byte-identical to what shipped before this
    extraction — the concrete proof that every existing caller/test is unaffected.
    """
    original = (
        "You are Ansina's Heart, a small always-on process. Every tick you decide, "
        "and only decide, what happens right now.\n"
        "\n"
        "Current state:\n"
        "{state}\n"
        "\n"
        "Reply with exactly one word: idle, act, or escalate.\n"
        "- idle: nothing needs attention right now.\n"
        "- act: something needs attention and you can handle it yourself.\n"
        "- escalate: something needs attention beyond your capability; hand off to "
        "the Brain.\n"
    )
    assert original == DEFAULT_TEMPLATE
