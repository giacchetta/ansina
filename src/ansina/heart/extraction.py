"""Gated experiment: Heart as structured extractor. See issue #15.

**Not wired into anything.** No route, no tick-loop caller, no
`OpencodeInterventionHandler` change — this module exists purely so
`heart/eval/extraction_runner.py` has something to bench. Sibling of
`heart/triage.py`/`heart/curation.py` (same "sibling duty to `heart/tick/`,
deliberately not inside it" posture issue #13's own module docstring establishes).

Unparked into M9 for a specific reason, unlike #13/#14's own general "prove it
before it ships" framing: issue #79's `OpencodeInterventionHandler` deliberately
uses a *deterministic* `InterventionPolicy` (`heart.tick
.opencode_intervention_handler.DeterministicInterventionPolicy`) — the Heart's
model output there is consumed only as "act / don't act," and code picks which
bounded action fires on which run. A model-chosen policy would need the Heart to
reliably emit a small, schema-shaped object instead. This module is the benchmark
that answers whether that's realistic, for three small jobs:

1. **Intervention selection** — `extract_intervention()`. The one job that
   actually gates a future policy swap: `{action, run_id, reason}`, scored for
   agreement with `DeterministicInterventionPolicy`'s own real priority ladder
   (the shipped baseline, not "the Brain would do it").
2. **Run titling** — `extract_title()`. A short session title from a run's
   prompt plus its first events. Measured and reported, but gated **separately**
   — a pass here never licenses swapping the intervention policy.
3. **Failure classification** — `extract_failure_cause()`.
   `{cause, evidence_event_seq}` over a failed run's event stream. Gated
   separately, the same as job 2.

`OpencodeAction` (job 1) is imported and reused **verbatim** — never relaxed,
never re-declared — the issue's own explicit "Do not" clause. This is the one
deliberate exception to `heart.curation`'s own "`ansina.heart` stays free of a
concrete `ansina.opencode` import" discipline: `OpencodeAction` is a closed
*enum*, not a domain object, the identical exception `heart.tick
.opencode_intervention_handler` already makes for the same reason (the heart
importing down-stack into `ansina.opencode` for a closed vocabulary is the same
acyclic direction `brain_escalation_handler.py` -> `ansina.brain` already takes).
`InterventionCandidateRun`/`CurationEvent` stay local/reused types — never a
concrete `ansina.opencode.repository.OpencodeRun`/`ansina.opencode.events
.RunEvent` import — mirroring `heart.curation.CurationEvent`'s own reasoning
exactly.

Every one of the three strict parsers below **never coerces a best guess** — the
issue's own explicit instruction, and the identical discipline `heart.curation
.try_parse_curation`/`heart.triage.try_parse_triage` already establish: an
unparseable, out-of-vocabulary, or hallucinated (a `run_id`/`evidence_event_seq`
not actually present in what the model was shown) reply resolves to `None`,
never a coerced default. There is no "production wrapper" pair (a `parse_*`
alongside each `try_parse_*`, the shape `heart.tick.decision`/`heart.triage`
use) — unlike those two duties, this port has no live caller to safe-default
for; `HeartStructuredExtractor`'s own methods log and return the `None`
directly, the same inline-fallback shape `HeartContextCurator.curate()` already
uses rather than routing through a second wrapper function.

`InterventionExtraction.action` is `OpencodeAction | None` — `None` means the model
*explicitly* answered "no intervention needed right now" (via the literal JSON value
`"none"`), a real, schema-valid answer distinct from `InterventionExtractionOutcome
.parsed is None` (a parse failure, one level up). This is necessary because
`DeterministicInterventionPolicy.decide()` itself can legitimately return `None` (no
candidate, no fault) — the bench needs the model to be able to agree with that outcome
too, not just with a concrete action.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from ansina.heart.curation import CurationEvent, render_event
from ansina.heart.runtime import HeartRuntime
from ansina.logging import get_logger
from ansina.opencode.interventions import OpencodeAction

logger = get_logger(__name__)

_MAX_REASON_LENGTH = 200
_DEFAULT_MAX_TITLE_LENGTH = 80


class FailureCause(StrEnum):
    """The closed label set job 3's `cause` field is validated against.

    Ships with a conservative starting set derived from the failure patterns issue
    #14's own real captured OpenCode sessions already observed (every one of its four
    fixtures was a shell or filesystem permission denial) — `OTHER` is the mandatory
    forward-compat fallback, the identical "intentionally small, extend against real
    data" discipline `ansina.opencode.events.RunEventType` already documents for its
    own `_RAW_TYPE_TO_EVENT_TYPE` table. A future real bench run against a wider real
    corpus is expected to add members here, never to relax the closed-set validation
    itself.
    """

    SHELL_DENIED = "shell_denied"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    ERROR = "error"
    OTHER = "other"


class ExtractionJob(StrEnum):
    """The discriminator both the fixture file's own `"job"` field and
    `EXTRACTION_PROMPT_VARIANTS`'s nested per-variant mapping key on.
    """

    INTERVENTION = "intervention"
    TITLE = "title"
    FAILURE_CAUSE = "failure_cause"


@dataclass(frozen=True, slots=True)
class InterventionCandidateRun:
    """One `opencode_runs`-shaped candidate the Heart is shown — reconstructed from
    real `OpencodeRun`/`heart.tick.sources.opencode_state.RunningRunState` fields,
    deliberately local rather than importing either (see module docstring).
    """

    run_id: str
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class InterventionExtraction:
    """See module docstring's own note on why `action` is nullable."""

    action: OpencodeAction | None
    run_id: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class FailureExtraction:
    cause: FailureCause
    evidence_event_seq: int


@dataclass(frozen=True, slots=True)
class InterventionExtractionOutcome:
    candidates: tuple[InterventionCandidateRun, ...]
    parsed: InterventionExtraction | None
    raw_output: str
    prompt_tokens: int

    @property
    def parse_fallback(self) -> bool:
        return self.parsed is None


@dataclass(frozen=True, slots=True)
class TitleExtractionOutcome:
    prompt: str
    parsed: str | None
    raw_output: str
    prompt_tokens: int

    @property
    def parse_fallback(self) -> bool:
        return self.parsed is None


@dataclass(frozen=True, slots=True)
class FailureExtractionOutcome:
    events: tuple[CurationEvent, ...]
    parsed: FailureExtraction | None
    raw_output: str
    prompt_tokens: int

    @property
    def parse_fallback(self) -> bool:
        return self.parsed is None


@runtime_checkable
class StructuredExtractor(Protocol):
    """The port a structured-extraction implementation satisfies — structural, like
    `HeartRuntime`/`BrainProvider`/`RequestTriage`/`ContextCurator` elsewhere in this
    codebase. One port, three operations — mirroring `HeartRuntime`'s own shape
    rather than three separate single-method ports, since all three are facets of
    one duty (can the Heart emit reliable structured output at all).
    """

    def extract_intervention(
        self,
        candidates: Sequence[InterventionCandidateRun],
        *,
        intake_paused: bool,
        recent_failed: int,
        recent_timeout: int,
        recent_permission_denied: int,
    ) -> InterventionExtractionOutcome:
        """Blocking — same discipline as `HeartRuntime.generate`; an event-loop
        caller must offload via `anyio.to_thread.run_sync`, never await directly.
        """
        ...

    def extract_title(
        self, prompt: str, events: Sequence[CurationEvent]
    ) -> TitleExtractionOutcome:
        """Blocking, same discipline."""
        ...

    def extract_failure_cause(
        self, events: Sequence[CurationEvent]
    ) -> FailureExtractionOutcome:
        """Blocking, same discipline."""
        ...


def _first_balanced_object(text: str) -> str | None:
    """The first balanced `{...}` substring in `text`, or `None` if there is no
    opening brace or it never closes. A plain brace-counting scan, not a regex —
    this codebase's reply payloads carry no nested objects, but a greedy/non-greedy
    regex alternative would either over- or under-match regardless.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _parse_json_object(raw: str) -> dict[str, object] | None:
    """Strips everything up to and including the last `</think>` first (the same
    measured justification `heart.tick.decision.try_parse_decision` documents),
    then tries the whole remaining text as JSON, falling back to the first balanced
    `{...}` substring (a model sometimes wraps its answer in a code fence or a short
    preamble despite being told not to). Returns `None` on anything that doesn't
    parse to a JSON object either way — never a coerced guess.
    """
    after_thinking = raw.rsplit("</think>", 1)[-1].strip()
    candidates = [after_thinking]
    balanced = _first_balanced_object(after_thinking)
    if balanced is not None and balanced != after_thinking:
        candidates.append(balanced)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def try_parse_intervention(
    raw: str, *, valid_run_ids: frozenset[str]
) -> InterventionExtraction | None:
    """`None` on malformed JSON, a missing/non-string `action`, an `action` that is
    neither the literal `"none"` nor a real `OpencodeAction` member (out-of-vocabulary
    — the issue's own zero-tolerance ceiling), or a non-null `run_id` not present in
    `valid_run_ids` (a hallucinated run id — the issue's other zero-tolerance
    ceiling). `reason` is truncated, never rejected, on an over-long value.
    """
    obj = _parse_json_object(raw)
    if obj is None:
        return None

    action_raw = obj.get("action")
    if not isinstance(action_raw, str):
        return None
    action: OpencodeAction | None
    if action_raw.strip().lower() == "none":
        action = None
    else:
        try:
            action = OpencodeAction(action_raw)
        except ValueError:
            return None

    run_id = obj.get("run_id")
    if run_id is not None and (
        not isinstance(run_id, str) or run_id not in valid_run_ids
    ):
        return None

    reason_raw = obj.get("reason", "")
    reason = reason_raw if isinstance(reason_raw, str) else ""
    return InterventionExtraction(
        action=action, run_id=run_id, reason=reason[:_MAX_REASON_LENGTH]
    )


_SINGLE_LINE_PATTERN = re.compile(r"\S")


def try_parse_title(
    raw: str, *, max_length: int = _DEFAULT_MAX_TITLE_LENGTH
) -> str | None:
    """`None` on an empty, multi-line, or over-length reply. Otherwise the stripped
    single line, verbatim. Unlike the JSON-schema jobs, there is no vocabulary to
    hallucinate against — "schema-valid" here means "a short, single-line title,"
    nothing more; judging title *quality* against the fixture's own human reference
    is left to a human reading `raw_output` side by side with it, never an automated
    semantic grader this codebase has no way to verify.
    """
    after_thinking = raw.rsplit("</think>", 1)[-1]
    stripped = after_thinking.strip()
    if not stripped:
        return None
    if "\n" in stripped:
        return None
    if len(stripped) > max_length:
        return None
    return stripped


def try_parse_failure(
    raw: str, *, valid_sequences: frozenset[int]
) -> FailureExtraction | None:
    """`None` on malformed JSON, a `cause` that isn't a real `FailureCause` member,
    or an `evidence_event_seq` that isn't an int present in `valid_sequences` (a
    hallucinated sequence number — the same zero-tolerance ceiling
    `try_parse_intervention` applies to a hallucinated `run_id`). `bool` is
    explicitly excluded from "is an int" — `isinstance(True, int)` is `True` in
    Python, and a model emitting `true`/`false` is not a valid sequence number.
    """
    obj = _parse_json_object(raw)
    if obj is None:
        return None

    cause_raw = obj.get("cause")
    if not isinstance(cause_raw, str):
        return None
    try:
        cause = FailureCause(cause_raw)
    except ValueError:
        return None

    seq_raw = obj.get("evidence_event_seq")
    if isinstance(seq_raw, bool) or not isinstance(seq_raw, int):
        return None
    if seq_raw not in valid_sequences:
        return None

    return FailureExtraction(cause=cause, evidence_event_seq=seq_raw)


def _render_candidate(candidate: InterventionCandidateRun) -> str:
    return f"[{candidate.run_id}] {candidate.status}: {candidate.detail}"


def _render_intervention_state(
    candidates: Sequence[InterventionCandidateRun],
    *,
    intake_paused: bool,
    recent_failed: int,
    recent_timeout: int,
    recent_permission_denied: int,
) -> str:
    """The one place both the extractor's own prompt and a future caller would
    measure/render OpenCode state from — mirroring
    `heart.tick.sources.opencode_state.OpencodeStateSource`'s own text-rendering
    voice (`"OpenCode: N queued, N running. Intake <state>."`) for consistency, not
    by import (that source reports a snapshot item, not a model-facing prompt
    block).
    """
    lines = [_render_candidate(c) for c in candidates] or [
        "(no active or recent OpenCode runs)"
    ]
    intake = "paused" if intake_paused else "accepting new runs"
    return (
        "\n".join(lines)
        + f"\nIntake: {intake}."
        + f"\nRecent outcomes: {recent_failed} failed, {recent_timeout} timed out, "
        f"{recent_permission_denied} permission denial(s)."
    )


_INTERVENTION_BASELINE_TEMPLATE = """\
You are supervising an OpenCode coding-agent executor on Ansina's behalf. Decide \
whether it needs one bounded intervention right now, and if so, which one.

State:
{state}

Valid actions: retry, nudge, compact, pause_intake, escalate_to_brain, or none.
- retry: resubmit a recent failed/timed-out run's own prompt fresh.
- nudge: send a short continuation to a running run that has stalled.
- compact: compact a run's own context (rarely applicable).
- pause_intake: stop accepting new runs (for a persistent, repeated failure pattern).
- escalate_to_brain: hand the situation to a more capable model.
- none: nothing needs attention right now.

Reply with exactly one JSON object with keys "action", "run_id" (one of the run ids \
shown in square brackets above, or null for pause_intake/escalate_to_brain/none), \
and "reason" (a short phrase). Nothing else.
"""

# Terse and rule-first, mirroring `heart.triage._STRICT_TEMPLATE`'s own framing
# (itself inherited from issue #53's tick bake-off) — an explicit ban on preamble
# targets a reasoning-tuned model's tendency to hedge or think out loud before its
# actual answer. An *inherited prior*, not yet proven for this duty: it is the
# bench's own job to confirm or refute that the same framing wins here too.
_INTERVENTION_STRICT_TEMPLATE = """\
Task: decide whether the OpenCode executor below needs exactly one bounded \
intervention right now, and if so, which one.

State:
{state}

Rules:
- Valid "action" values: retry, nudge, compact, pause_intake, escalate_to_brain, none.
- "run_id" must be one of the run ids shown above in square brackets, or null — \
never invent an id that is not shown above.
- If nothing needs attention, use "action": "none" and "run_id": null.
- Output only a single JSON object with keys "action", "run_id", "reason" (a short \
phrase). No explanation, no reasoning, no preamble, no code fence.
"""

_TITLE_BASELINE_TEMPLATE = """\
You are titling an OpenCode coding-agent run for a human reviewing a list of runs. \
Here is the run's own request plus its first events.

Request:
{prompt}

First events:
{events}

Reply with a short, single-line title (a few words) describing what this run is \
doing. Nothing else.
"""

_TITLE_STRICT_TEMPLATE = """\
Task: write a short, single-line title for the OpenCode run below, from its own \
request and first events.

Request:
{prompt}

First events:
{events}

Rules:
- Output only the title itself: a single line, no more than a dozen words.
- No explanation, no reasoning, no preamble, no quotation marks, no code fence.
"""

_FAILURE_BASELINE_TEMPLATE = """\
Here is the full event transcript of an OpenCode coding-agent run that did not \
complete cleanly, each line numbered.

Transcript:
{events}

What caused it to end the way it did? Reply with exactly one JSON object with keys \
"cause" (one of: shell_denied, permission_denied, timeout, error, other) and \
"evidence_event_seq" (the number of the one event line that best shows this). \
Nothing else.
"""

_FAILURE_STRICT_TEMPLATE = """\
Task: classify why the OpenCode run below did not complete cleanly, from its \
numbered event transcript.

Transcript:
{events}

Rules:
- "cause" must be exactly one of: shell_denied, permission_denied, timeout, error, \
other.
- "evidence_event_seq" must be the number of one event line shown above that best \
shows the cause — never a number not shown above.
- Output only a single JSON object with keys "cause", "evidence_event_seq". No \
explanation, no reasoning, no preamble, no code fence.
"""

EXTRACTION_PROMPT_VARIANTS: Mapping[str, Mapping[ExtractionJob, str]] = (
    MappingProxyType(
        {
            "baseline": MappingProxyType(
                {
                    ExtractionJob.INTERVENTION: _INTERVENTION_BASELINE_TEMPLATE,
                    ExtractionJob.TITLE: _TITLE_BASELINE_TEMPLATE,
                    ExtractionJob.FAILURE_CAUSE: _FAILURE_BASELINE_TEMPLATE,
                }
            ),
            "strict": MappingProxyType(
                {
                    ExtractionJob.INTERVENTION: _INTERVENTION_STRICT_TEMPLATE,
                    ExtractionJob.TITLE: _TITLE_STRICT_TEMPLATE,
                    ExtractionJob.FAILURE_CAUSE: _FAILURE_STRICT_TEMPLATE,
                }
            ),
        }
    )
)

# Inherited from issue #53/#13/#14's own bake-off winners — not yet proven for this
# duty. The first real extraction bench run on the Mac Mini M4 is what actually
# validates (or refutes) carrying this choice over; see `heart/eval/extraction_runner
# .py`.
DEFAULT_EXTRACTION_VARIANT = "strict"
DEFAULT_EXTRACTION_TEMPLATES = EXTRACTION_PROMPT_VARIANTS[DEFAULT_EXTRACTION_VARIANT]


class HeartStructuredExtractor:
    """The one `StructuredExtractor` implementation, over a `HeartRuntime`.

    `runtime` must already be loaded (`runtime.load()`) — this class never calls
    it, the same division of responsibility `heart.triage.HeartRequestTriage`/
    `heart.curation.HeartContextCurator` already document for their own
    `HeartRuntime` parameter. No `BrainProvider` dependency anywhere in this class —
    unlike `HeartContextCurator`, every job here is graded against a fixture's own
    human-authored or deterministic-policy ground truth, never a live Brain round
    trip.
    """

    def __init__(
        self,
        runtime: HeartRuntime,
        *,
        templates: Mapping[ExtractionJob, str] = DEFAULT_EXTRACTION_TEMPLATES,
        max_output_tokens: int | None = None,
        max_title_length: int = _DEFAULT_MAX_TITLE_LENGTH,
    ) -> None:
        self._runtime = runtime
        self._templates = templates
        self._max_output_tokens = max_output_tokens
        self._max_title_length = max_title_length

    def _generate(self, prompt: str) -> tuple[str, int]:
        raw = self._runtime.generate(prompt, max_tokens=self._max_output_tokens)
        return raw, self._runtime.token_count(prompt)

    def extract_intervention(
        self,
        candidates: Sequence[InterventionCandidateRun],
        *,
        intake_paused: bool,
        recent_failed: int,
        recent_timeout: int,
        recent_permission_denied: int,
    ) -> InterventionExtractionOutcome:
        candidates_tuple = tuple(candidates)
        valid_run_ids = frozenset(c.run_id for c in candidates_tuple)
        state = _render_intervention_state(
            candidates_tuple,
            intake_paused=intake_paused,
            recent_failed=recent_failed,
            recent_timeout=recent_timeout,
            recent_permission_denied=recent_permission_denied,
        )
        prompt = self._templates[ExtractionJob.INTERVENTION].format(state=state)
        raw, prompt_tokens = self._generate(prompt)
        parsed = try_parse_intervention(raw, valid_run_ids=valid_run_ids)
        if parsed is None:
            logger.warning(
                "heart extraction: unparseable intervention reply", extra={"raw": raw}
            )
        return InterventionExtractionOutcome(
            candidates=candidates_tuple,
            parsed=parsed,
            raw_output=raw,
            prompt_tokens=prompt_tokens,
        )

    def extract_title(
        self, prompt: str, events: Sequence[CurationEvent]
    ) -> TitleExtractionOutcome:
        events_tuple = tuple(events)
        rendered_events = "\n".join(render_event(e) for e in events_tuple)
        rendered_prompt = self._templates[ExtractionJob.TITLE].format(
            prompt=prompt, events=rendered_events
        )
        raw, prompt_tokens = self._generate(rendered_prompt)
        parsed = try_parse_title(raw, max_length=self._max_title_length)
        if parsed is None:
            logger.warning(
                "heart extraction: unparseable title reply", extra={"raw": raw}
            )
        return TitleExtractionOutcome(
            prompt=prompt,
            parsed=parsed,
            raw_output=raw,
            prompt_tokens=prompt_tokens,
        )

    def extract_failure_cause(
        self, events: Sequence[CurationEvent]
    ) -> FailureExtractionOutcome:
        events_tuple = tuple(events)
        valid_sequences = frozenset(e.sequence for e in events_tuple)
        rendered_events = "\n".join(render_event(e) for e in events_tuple)
        rendered_prompt = self._templates[ExtractionJob.FAILURE_CAUSE].format(
            events=rendered_events
        )
        raw, prompt_tokens = self._generate(rendered_prompt)
        parsed = try_parse_failure(raw, valid_sequences=valid_sequences)
        if parsed is None:
            logger.warning(
                "heart extraction: unparseable failure-cause reply", extra={"raw": raw}
            )
        return FailureExtractionOutcome(
            events=events_tuple,
            parsed=parsed,
            raw_output=raw,
            prompt_tokens=prompt_tokens,
        )
