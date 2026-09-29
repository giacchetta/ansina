from __future__ import annotations

from ansina.heart.tick.prompts import (
    DEFAULT_PROMPT_VARIANT,
    DEFAULT_TEMPLATE,
    NO_PENDING_STATE,
    PROMPT_VARIANTS,
)


def test_default_variant_is_strict() -> None:
    """Issue #53's bench winner (75% vs. baseline's 62.5% and fewshot's 50% on the
    smallest ladder rung) — the variant actually used by the gate-clearing
    `HeartSettings.model_repo` default, so a caller taking neither explicitly gets
    the validated combination, not an untested one.
    """
    assert DEFAULT_PROMPT_VARIANT == "strict"
    assert DEFAULT_TEMPLATE is PROMPT_VARIANTS["strict"]


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
    """The "baseline" variant itself (not `DEFAULT_TEMPLATE`, which issue #53's own
    bench result later repointed to "strict") must stay byte-identical to what
    `build_prompt` inlined before this extraction — the concrete proof that no
    existing caller/test depended on wording this module could have silently
    drifted.
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
    assert original == PROMPT_VARIANTS["baseline"]
