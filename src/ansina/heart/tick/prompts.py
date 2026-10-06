"""Named tick-prompt variants. See issue #53.

`snapshot.py`'s `build_prompt` originally hardcoded one template. This module lifts it
out as `"baseline"` (byte-identical to what shipped, so the default stays proven
unchanged — issue #53's own AC) and adds two more framings, so a prompt A/B test is a
bench parameter (`heart.eval.runner`) rather than an edit to the tick loop itself.

Every variant must contain exactly one `{state}` placeholder — `build_prompt` fills it
via `str.format`, the same as the original inline template did.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

NO_PENDING_STATE = "(no pending state)"

_BASELINE = """\
You are Ansina's Heart, a small always-on process. Every tick you decide, and only \
decide, what happens right now.

Current state:
{state}

Reply with exactly one word: idle, act, or escalate.
- idle: nothing needs attention right now.
- act: something needs attention and you can handle it yourself.
- escalate: something needs attention beyond your capability; hand off to the Brain.
"""

# Terse and rule-first, with an explicit ban on preamble/explanation — targets a
# reasoning-tuned model's tendency to emit a `<think>...</think>` block or a hedge
# before its actual answer, which `heart.tick.decision.parse_decision` would otherwise
# misread as an unparseable reply and silently default to `idle`.
#
# Issue #54's act/escalate rule was sharpened on the real Mac Mini M4 (see the
# "before" evidence — `kind=bench/dt=2026-09-30/2026-09-30-gemma-4-e2b-it-4bit-
# strict-preimprovement.{md,json}` in the report bucket, issue #59; `docs/heart/
# bench/` is gitignored, so that bucket key, not a local path, is the resolvable
# reference once `make heart-bench-publish` has run): #53's original wording
# ("act: something needs attention and you can handle it yourself" / "escalate:
# ... beyond your capability") left the model with no way to judge severity once
# #54's daemon-self-state fixtures gave it real, nameable conditions to weigh
# against each other, and it broke toward "escalate" on several single, routine
# issues (80.6% accuracy, gate FAIL). The rule below keeps #53's original
# capability-based clause (still needed for e.g. "a critical CVE was just
# disclosed" — a single item, but a judgment call) and adds an explicit counting
# heuristic on top: exactly one routine problem is `act`, two or more simultaneous
# problems (or one that keeps recurring despite retries) is `escalate`. Re-measured
# at 97.2% accuracy, gate PASS — see the plain-named report at the same bucket key
# (no `-preimprovement` suffix), or `docs/heart/findings.md` for the numbers either
# way.
_STRICT = """\
Task: classify the current state as exactly one of idle, act, escalate.

Rules:
- idle: nothing needs attention right now.
- act: exactly one specific problem needs attention, and it has an obvious, routine \
fix you can apply yourself.
- escalate: something needs attention beyond your capability (a judgment call, a \
security concern) — or two or more problems are happening at once, or one problem \
keeps recurring despite already being retried. Hand off to the Brain.

State:
{state}

Output only the word. No explanation, no reasoning, no preamble.
"""

# Baseline plus one worked example per class — tests whether a small model follows the
# instruction more reliably when shown the shape of a correct answer.
_FEWSHOT = """\
You are Ansina's Heart, a small always-on process. Every tick you decide, and only \
decide, what happens right now.

Examples:
State: (no pending state)
Answer: idle

State: A scheduled backup job is due and you can run it yourself.
Answer: act

State: A dependency has a critical security advisory requiring a judgment call.
Answer: escalate

Current state:
{state}

Reply with exactly one word: idle, act, or escalate.
- idle: nothing needs attention right now.
- act: something needs attention and you can handle it yourself.
- escalate: something needs attention beyond your capability; hand off to the Brain.
"""

PROMPT_VARIANTS: Mapping[str, str] = MappingProxyType(
    {
        "baseline": _BASELINE,
        "strict": _STRICT,
        "fewshot": _FEWSHOT,
    }
)

# Issue #53's bench (`docs/heart/bench/`, published to the report bucket as of
# issue #59 — see that issue's own `heart/eval/` AGENTS.md entry) benched all
# three variants against the
# smallest ladder rung and carried the winner up the ladder: "strict" beat
# "baseline" (75% vs. 62.5% accuracy) and "fewshot" (50%) there, and is the variant
# actually used by the gate-clearing `HeartSettings.model_repo` default above —
# "baseline" stays defined (and byte-identical to what shipped before this issue)
# for future A/B comparisons, it just isn't what ships by default anymore.
DEFAULT_PROMPT_VARIANT = "strict"
DEFAULT_TEMPLATE = PROMPT_VARIANTS[DEFAULT_PROMPT_VARIANT]
