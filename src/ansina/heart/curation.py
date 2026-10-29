"""Gated experiment: Heart as context curator. See issue #14.

**Not wired into anything.** No route, no tick-loop caller, no Brain-escalation-path
change — this module exists purely so `heart/eval/curation_runner.py` has something
to bench. Sibling of `heart/triage.py` (same "sibling duty to `heart/tick/`,
deliberately not inside it" posture issue #13's own module docstring establishes) —
only once M9's OpenCode sessions gave Ansina its first real multi-event corpus to
curate did this duty have anything concrete to bench against (the tick loop's own
prompt stays bounded to five `SnapshotItem`s, and `heart_journal` rows are one line
each — neither is a curation problem).

Given one OpenCode run's own session (a sequence of real, captured `kind=opencode/`
events), the Heart is asked to *select* a subset that fits a token budget — never to
author summary text (that is issue #15's territory, with its own gate). The baseline
this experiment is measured against is **recency-window truncation**
(`recency_window_selection` below) — exactly what `heart.tick.sources.recent_journal.
RecentJournalSource` already does for journal rows, so the baseline is a real, shipped
behavior rather than a strawman.

`CurationEvent` is a small, **local** type — deliberately not an import of
`ansina.opencode.events.RunEvent` — the same reasoning
`heart.tick.sources.opencode_state`'s own structural-Protocol approach documents:
`ansina.heart` stays free of an `ansina.opencode` import. `heart.eval.fixtures`
constructs these directly from the committed, already-redacted/truncated JSONL fields.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from ansina.heart.runtime import HeartRuntime
from ansina.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CurationEvent:
    """One line of a real, captured OpenCode run's event stream, as reconstructed
    from the committed fixture set — `sequence` is the same 0-based, per-run ordinal
    `ansina.opencode.events.RunEvent.sequence` carries, `raw_type`/`payload` the same
    already-redacted/truncated fields that module's `parse_run_event` produces.
    """

    sequence: int
    raw_type: str
    payload: str


def render_event(event: CurationEvent) -> str:
    """The one place both the curator's own prompt and the recency baseline measure
    tokens from — pure, so the two can never silently disagree about what an event
    "costs."
    """
    return f"[{event.sequence}] {event.raw_type}: {event.payload}"


def recency_window_selection(
    events: Sequence[CurationEvent],
    *,
    budget_tokens: int,
    token_count: Callable[[str], int],
) -> frozenset[int]:
    """The baseline this whole experiment is measured against (issue #14's own
    "Baseline" section): walk `events` newest-first, keep adding while the cumulative
    rendered-token cost stays within `budget_tokens`, stop at the first event that
    would exceed it — never skipping ahead to a smaller, older one. Pure, no model
    call.

    Also reused as `HeartContextCurator`'s own safe-default fallback on an
    unparseable reply, so the fallback path and the explicit baseline condition can
    never silently drift apart from each other.
    """
    newest_first = sorted(events, key=lambda event: event.sequence, reverse=True)
    cumulative = 0
    kept: set[int] = set()
    for event in newest_first:
        cost = token_count(render_event(event))
        if cumulative + cost > budget_tokens:
            break
        cumulative += cost
        kept.add(event.sequence)
    return frozenset(kept)


@dataclass(frozen=True, slots=True)
class CurationOutcome:
    """One `ContextCurator.curate()` call's result.

    `selected` is always safe-defaulted (falls back to `recency_window_selection` on
    an unparseable reply — the same "default to the known-safe choice" discipline
    `heart.tick.decision.parse_decision` applies). `parsed` is the model's own raw,
    budget-*unenforced* selection — `None` iff the reply was completely unparseable,
    the same `parse_fallback` distinction `TriageOutcome`/`FixtureResult` already
    draw. `exceeded_budget` is `True` when a non-`None` `parsed` selection didn't fit
    `budget_tokens` on its own and had to be trimmed to produce `selected` — trimmed
    by walking the model's own listed order and dropping from the point the budget is
    exceeded, rewarding a model that lists its most essential picks first.
    """

    events: tuple[CurationEvent, ...]
    selected: frozenset[int]
    parsed: frozenset[int] | None
    raw_output: str
    prompt_tokens: int
    exceeded_budget: bool

    @property
    def parse_fallback(self) -> bool:
        return self.parsed is None


@runtime_checkable
class ContextCurator(Protocol):
    """The port a context-curation implementation satisfies — structural, like
    `HeartRuntime`/`BrainProvider`/`RequestTriage` elsewhere in this codebase.
    """

    def curate(
        self, events: Sequence[CurationEvent], *, budget_tokens: int
    ) -> CurationOutcome:
        """Select a subset of `events` that fits `budget_tokens`. Blocking — same
        discipline as `HeartRuntime.generate`/`RequestTriage.classify`; an event-loop
        caller must offload via `anyio.to_thread.run_sync`, never await this directly.
        """
        ...


_INT_PATTERN = re.compile(r"\d+")


def try_parse_curation(
    raw: str, *, valid_sequences: frozenset[int]
) -> tuple[int, ...] | None:
    """The event numbers named in `raw`, in first-mentioned order (deduplicated,
    filtered to `valid_sequences`), or `None` if none of the integers `raw` contains
    are a real sequence number.

    Pure — no logging, no default. Sibling of `heart.tick.decision.
    try_parse_decision`/`heart.triage.try_parse_triage`, sharing their exact
    `</think>`-stripping mechanic (a reasoning model's real answer lands after its
    own closing think tag — see `try_parse_decision`'s docstring for the measured
    justification). Unlike those two (a fixed 3-word vocabulary), this extracts every
    integer substring via `re.finditer` rather than taking "the first word" — a
    curator's reply is a list, not a single token.

    Returned as an **ordered tuple**, not a `frozenset`, because
    `HeartContextCurator.curate()` needs the model's own listed order to enforce the
    token budget (keep the earliest-listed picks, drop the rest) — callers that only
    care about membership (e.g. `parse_curation` below) wrap the result in
    `frozenset(...)` themselves.
    """
    after_thinking = raw.rsplit("</think>", 1)[-1]
    seen: set[int] = set()
    ordered: list[int] = []
    for match in _INT_PATTERN.finditer(after_thinking):
        value = int(match.group())
        if value in valid_sequences and value not in seen:
            seen.add(value)
            ordered.append(value)
    if not ordered:
        return None
    return tuple(ordered)


def parse_curation(
    raw: str, *, valid_sequences: frozenset[int], fallback: frozenset[int]
) -> frozenset[int]:
    """The event numbers named in `raw`, defaulting to `fallback` (a
    `recency_window_selection` call, by convention) on an unparseable reply.

    Mirrors `heart.tick.decision.parse_decision`/`heart.triage.parse_triage`'s
    "default to the safe choice" discipline — for curation the safe default is
    "behave exactly like the baseline," since that is this experiment's own known-good
    fallback, not an arbitrary empty/full selection. An unparseable reply logs a
    warning rather than silently defaulting, so a bad prompt or model is caught rather
    than misread.

    `HeartContextCurator.curate()` does **not** call this — it needs
    `try_parse_curation`'s own ordered result to enforce the token budget on a
    successful parse, which this defaulting wrapper deliberately doesn't expose. This
    function exists for the same parity `parse_decision`/`parse_triage` give their own
    `try_parse_*` siblings, and is what a caller with no budget-enforcement need (e.g.
    a test, or a future caller with a different trimming policy) should use.
    """
    parsed = try_parse_curation(raw, valid_sequences=valid_sequences)
    if parsed is None:
        logger.warning(
            "heart curation: unparseable reply, defaulting to recency baseline",
            extra={"raw": raw},
        )
        return fallback
    return frozenset(parsed)


def _enforce_budget(
    events: Sequence[CurationEvent],
    ordered_sequences: Sequence[int],
    *,
    budget_tokens: int,
    token_count: Callable[[str], int],
) -> tuple[frozenset[int], bool]:
    """Trims `ordered_sequences` (the model's own selection, in its own listed order)
    to fit `budget_tokens`, dropping from the point the running total would exceed it
    — never reordering, so a model that lists its most essential picks first loses
    only its lowest-priority ones. Returns `(kept, exceeded)`.
    """
    by_sequence = {event.sequence: event for event in events}
    cumulative = 0
    kept: list[int] = []
    exceeded = False
    for sequence in ordered_sequences:
        event = by_sequence[sequence]
        cost = token_count(render_event(event))
        if cumulative + cost > budget_tokens:
            exceeded = True
            break
        cumulative += cost
        kept.append(sequence)
    return frozenset(kept), exceeded


_BASELINE_TEMPLATE = """\
You are curating context for a more capable assistant that will be asked to explain \
why an OpenCode coding-agent run ended the way it did. Below is the run's full event \
transcript, each line numbered.

Transcript:
{events}

Select at most {max_events} event numbers — the ones most essential to explaining the \
outcome, not simply the most recent ones. Reply with exactly those numbers, \
comma-separated, and nothing else.
"""

# Terse and rule-first, mirroring `heart.triage._STRICT_TEMPLATE`'s own framing
# (itself inherited from issue #53's tick bake-off) — an explicit ban on preamble
# targets a reasoning-tuned model's tendency to hedge or think out loud before its
# actual answer. An *inherited prior*, not yet proven for this duty: it is the bench's
# own job to confirm or refute that the same framing that won the tick/triage bake-offs
# also wins here.
_STRICT_TEMPLATE = """\
Task: from the numbered event transcript below, select at most {max_events} event \
numbers essential to explaining why the run ended the way it did.

Transcript:
{events}

Rules:
- Prefer the events that explain the cause, not simply the most recent ones.
- Output only a comma-separated list of the selected event numbers. No explanation, \
no reasoning, no preamble.
"""

CURATION_PROMPT_VARIANTS: Mapping[str, str] = MappingProxyType(
    {
        "baseline": _BASELINE_TEMPLATE,
        "strict": _STRICT_TEMPLATE,
    }
)

# Inherited from issue #53/#13's own bake-off winners — not yet proven for this duty.
# The first real curation bench run on the Mac Mini M4 is what actually validates (or
# refutes) carrying this choice over; see `heart/eval/curation_runner.py`.
DEFAULT_CURATION_VARIANT = "strict"
DEFAULT_CURATION_TEMPLATE = CURATION_PROMPT_VARIANTS[DEFAULT_CURATION_VARIANT]


class HeartContextCurator:
    """The one `ContextCurator` implementation, over a `HeartRuntime`.

    `runtime` must already be loaded (`runtime.load()`) — this class never calls it,
    the same division of responsibility `heart.triage.HeartRequestTriage` and
    `heart.eval.runner.run_bench` already document for their own `HeartRuntime`
    parameter.
    """

    def __init__(
        self,
        runtime: HeartRuntime,
        *,
        template: str = DEFAULT_CURATION_TEMPLATE,
        max_output_tokens: int | None = None,
    ) -> None:
        self._runtime = runtime
        self._template = template
        self._max_output_tokens = max_output_tokens

    def curate(
        self, events: Sequence[CurationEvent], *, budget_tokens: int
    ) -> CurationOutcome:
        events_tuple = tuple(events)
        valid_sequences = frozenset(event.sequence for event in events_tuple)
        rendered = [render_event(event) for event in events_tuple]
        per_event_tokens = [self._runtime.token_count(line) for line in rendered]
        # An approximate *event-count* target, not a literal token count, is what a
        # small instruct model can actually self-assess against — the real token
        # budget is enforced after the fact by `_enforce_budget`, regardless of
        # whether the model's own selection honored this estimate.
        average_tokens = (
            sum(per_event_tokens) / len(per_event_tokens) if per_event_tokens else 1.0
        )
        max_events = (
            max(1, math.floor(budget_tokens / average_tokens))
            if average_tokens > 0
            else len(events_tuple)
        )
        prompt = self._template.format(
            events="\n".join(rendered), max_events=max_events
        )
        raw = self._runtime.generate(prompt, max_tokens=self._max_output_tokens)
        prompt_tokens = self._runtime.token_count(prompt)
        fallback = recency_window_selection(
            events_tuple,
            budget_tokens=budget_tokens,
            token_count=self._runtime.token_count,
        )

        parsed_ordered = try_parse_curation(raw, valid_sequences=valid_sequences)
        if parsed_ordered is None:
            logger.warning(
                "heart curation: unparseable reply, defaulting to recency baseline",
                extra={"raw": raw},
            )
            return CurationOutcome(
                events=events_tuple,
                selected=fallback,
                parsed=None,
                raw_output=raw,
                prompt_tokens=prompt_tokens,
                exceeded_budget=False,
            )

        selected, exceeded = _enforce_budget(
            events_tuple,
            parsed_ordered,
            budget_tokens=budget_tokens,
            token_count=self._runtime.token_count,
        )
        return CurationOutcome(
            events=events_tuple,
            selected=selected,
            parsed=frozenset(parsed_ordered),
            raw_output=raw,
            prompt_tokens=prompt_tokens,
            exceeded_budget=exceeded,
        )
