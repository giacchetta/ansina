"""Gated experiment: Heart as request triage/router. See issue #13.

**Not wired into anything.** No route, no tick-loop caller, no `create_app` wiring —
this module exists purely so `heart/eval/triage_runner.py` has something to bench. The
issue's own "Do not" clause (restated by M7 planning) is explicit: fold this into a live
path only after the benchmark passes, and even then in a follow-up issue, not this one.

Ansina has no inbound-request surface at all (no `/chat`, no `POST /requests`, no
session domain, no `ToolRegistry`) — so there is nothing to classify in production yet.
This port exists to be benched against a synthetic, hand-labelled fixture set, the same
"prove it before it ships" posture issue #53 gave the tick-decision duty.

Routing is deliberately **2-way**, not 3-way: `TOOL_ONLY` routes to the Brain exactly
like `COMPLEX` does, because no tool executor exists yet to actually run anything
deterministically. The 3-way label is still captured and measured (`recall_by_class`,
the confusion matrix) so the metric stays reusable without relabelling the fixture set
the day a `ToolRegistry` lands — see `routes_to_brain` below, the one function that
changes when that day comes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from ansina.heart.runtime import HeartRuntime
from ansina.logging import get_logger

logger = get_logger(__name__)


class TriageClass(StrEnum):
    """The three-way label a triage fixture carries, and `try_parse_triage`'s target.

    Member values deliberately use a hyphen (`"tool-only"`) even though
    `try_parse_triage` also accepts an underscore/no-separator spelling in raw model
    output — the enum's own `.value` is what every fixture file, report, and prompt
    template renders.
    """

    TRIVIAL = "trivial"
    TOOL_ONLY = "tool-only"
    COMPLEX = "complex"


def routes_to_brain(triage_class: TriageClass) -> bool:
    """Whether `triage_class` should escalate to the Brain under **today's** 2-way
    routing. Only `TRIVIAL` answers without the Brain; `TOOL_ONLY` and `COMPLEX` both
    escalate, since no `ToolRegistry` exists yet to execute `TOOL_ONLY`
    deterministically.

    This is the one place that changes the day a tool layer lands — nothing else in
    this module encodes the 2-way-vs-3-way distinction.
    """
    return triage_class is not TriageClass.TRIVIAL


_SEPARATOR_VARIANTS: Mapping[str, TriageClass] = MappingProxyType(
    {
        "trivial": TriageClass.TRIVIAL,
        "tool-only": TriageClass.TOOL_ONLY,
        "tool_only": TriageClass.TOOL_ONLY,
        "toolonly": TriageClass.TOOL_ONLY,
        "complex": TriageClass.COMPLEX,
    }
)


def try_parse_triage(raw: str) -> TriageClass | None:
    """The first recognizable triage word in `raw`, or `None` if unparseable.

    Pure — no logging, no default. Sibling of `heart.tick.decision.try_parse_decision`,
    sharing its exact mechanics: strip everything up to and including the last
    `</think>` first (a reasoning model's real answer lands after its own closing think
    tag — see that function's docstring for the measured justification), then take the
    first punctuation-stripped word. Unlike the tick classes, `"tool-only"` contains
    its own separator, so a raw reply spelling it `tool_only` or `toolonly` (no
    separator) is also recognized — `_SEPARATOR_VARIANTS` is the one place that
    normalization lives.
    """
    after_thinking = raw.rsplit("</think>", 1)[-1]
    normalized = after_thinking.strip().lower()
    first_word = normalized.split(maxsplit=1)[0] if normalized else ""
    first_word = first_word.strip(".,:;!?\"'")
    return _SEPARATOR_VARIANTS.get(first_word)


def parse_triage(raw: str) -> TriageClass:
    """The first recognizable triage word in `raw`, defaulting to `COMPLEX`.

    Mirrors `heart.tick.decision.parse_decision`'s "default to the safe choice"
    discipline: for the tick loop the safe default is `IDLE` (do nothing); for triage
    the safe default is `COMPLEX` (always escalate to the Brain) — the baseline this
    whole experiment is measured against, and the one answer that can never be an
    under-route. An unparseable or ambiguous reply logs a warning rather than silently
    defaulting, so a bad prompt or model is caught rather than misread.
    """
    triage_class = try_parse_triage(raw)
    if triage_class is None:
        logger.warning(
            "heart triage: unparseable reply, defaulting to complex",
            extra={"raw": raw},
        )
        return TriageClass.COMPLEX
    return triage_class


@dataclass(frozen=True, slots=True)
class TriageOutcome:
    """One `RequestTriage.classify()` call's result.

    `triage_class` is always safe-defaulted (`parse_triage`'s `COMPLEX`-on-fallback
    behavior) — `parsed` is `None` iff the raw reply was unparseable, the same
    correct-vs-fallback distinction `heart.eval.runner.FixtureResult` draws for tick
    decisions, needed here so the triage bench can count a parse fallback separately
    from a genuine complex classification.
    """

    request: str
    triage_class: TriageClass
    parsed: TriageClass | None
    raw_output: str
    prompt_tokens: int

    @property
    def parse_fallback(self) -> bool:
        return self.parsed is None

    @property
    def routes_to_brain(self) -> bool:
        return routes_to_brain(self.triage_class)


@runtime_checkable
class RequestTriage(Protocol):
    """The port a request-triage implementation satisfies — structural, like
    `HeartRuntime`/`BrainProvider`/`StepUpVerifier` elsewhere in this codebase.
    """

    def classify(self, request: str) -> TriageOutcome:
        """Classify `request`. Blocking — same discipline as `HeartRuntime.generate`;
        an event-loop caller must offload via `anyio.to_thread.run_sync`, never await
        this directly.
        """
        ...


_BASELINE_TEMPLATE = """\
You are classifying an inbound request for Ansina, a self-owned AI agent. Decide which \
of three categories it falls into.

Request:
{request}

Reply with exactly one word: trivial, tool-only, or complex.
- trivial: answerable directly from your own knowledge, no state read or action needed.
- tool-only: needs exactly one deterministic state read or action, no judgment once it \
returns.
- complex: needs reasoning, synthesis, a trade-off, multi-step planning, or a \
security/judgment call.
"""

# Terse and rule-first, mirroring `heart.tick.prompts._STRICT`'s own framing (issue
# #53) — an explicit ban on preamble targets a reasoning-tuned model's tendency to
# hedge or think out loud before its actual answer. Unlike `_STRICT` there, this is
# an *inherited prior*, not yet proven for triage specifically: it is the bench's own
# job to confirm or refute that the same framing that won the tick bake-off also wins
# here, not an assumption this module makes on its behalf.
_STRICT_TEMPLATE = """\
Task: classify the request below as exactly one of trivial, tool-only, complex.

Request:
{request}

Rules:
- trivial: answerable directly from your own knowledge, no state read or action needed.
- tool-only: needs exactly one deterministic state read or action, no judgment once it \
returns.
- complex: needs reasoning, synthesis, a trade-off, multi-step planning, or a \
security/judgment call.

Output only the word. No explanation, no reasoning, no preamble.
"""

TRIAGE_PROMPT_VARIANTS: Mapping[str, str] = MappingProxyType(
    {
        "baseline": _BASELINE_TEMPLATE,
        "strict": _STRICT_TEMPLATE,
    }
)

# Inherited from issue #53's tick-decision bake-off winner — not yet proven for this
# duty. The first real triage bench run on the Mac Mini M4 is what actually validates
# (or refutes) carrying this choice over; see `heart/eval/triage_runner.py`.
DEFAULT_TRIAGE_VARIANT = "strict"
DEFAULT_TRIAGE_TEMPLATE = TRIAGE_PROMPT_VARIANTS[DEFAULT_TRIAGE_VARIANT]


class HeartRequestTriage:
    """The one `RequestTriage` implementation, over a `HeartRuntime`.

    `runtime` must already be loaded (`runtime.load()`) — this class never calls it,
    the same division of responsibility `heart.eval.runner.run_bench` already
    documents for its own `HeartRuntime` parameter. An over-long request is left to
    `BaseHeartRuntime.generate`'s own `HeartContextOverflowError` — refused loudly,
    never silently truncated, unlike `heart.tick.snapshot.build_prompt`, which is
    *designed* to trim a multi-item snapshot. A single request has no sub-items to
    drop.
    """

    def __init__(
        self,
        runtime: HeartRuntime,
        *,
        template: str = DEFAULT_TRIAGE_TEMPLATE,
        max_output_tokens: int | None = None,
    ) -> None:
        self._runtime = runtime
        self._template = template
        self._max_output_tokens = max_output_tokens

    def classify(self, request: str) -> TriageOutcome:
        prompt = self._template.format(request=request)
        raw = self._runtime.generate(prompt, max_tokens=self._max_output_tokens)
        parsed = try_parse_triage(raw)
        return TriageOutcome(
            request=request,
            triage_class=parsed if parsed is not None else parse_triage(raw),
            parsed=parsed,
            raw_output=raw,
            prompt_tokens=self._runtime.token_count(prompt),
        )
