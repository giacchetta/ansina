"""Labelled fixture sets for the bench harness. See issue #53 (tick) / #13 (triage).

A tick fixture is one hand-labelled tick scenario: the `SnapshotItem`s a real
`StateSnapshotSource` set would have contributed, and the `TickDecision` a correct
Heart should reach given them. `load_fixtures()` deserializes the bundled JSONL set
into real `heart.tick.snapshot.SnapshotItem`s, so `heart.eval.runner` renders every
fixture through the actual shipped `build_prompt` — never a parallel copy of it.

A triage fixture (issue #13) is one hand-labelled inbound-request scenario: the raw
request text and the `TriageClass` a correct triage should reach.
`load_triage_fixtures()` shares this module's line-parsing/comment-skipping/
duplicate-id/every-class-present machinery with `load_fixtures()` — the two differ
only in what one JSON object deserializes into, never in how the file as a whole is
read or validated.

A curation fixture (issue #14) is one real, captured OpenCode session: its event
transcript (`heart.curation.CurationEvent`s, reconstructed from real `kind=opencode/`
`run_event` lines — see that module's own docstring for why this is a local type, not
an `ansina.opencode.events.RunEvent` import), the sequence numbers a human judged
essential to explain the run's outcome (`must_keep`), and a short `failure_cause`
label the bench's keyword-match proxy scores a Brain answer against.
`load_curation_fixtures()` shares `_resolve_text`/`_iter_fixture_lines` with the two
loaders above, plus the standalone duplicate-id check
(`_check_no_duplicate_ids`) — but **not** `_validate_fixture_set`'s "every class
present" half, since there is no label enum to be exhaustive over.

All three loaders read via `importlib.resources`, not `Path(__file__)` — the same
pattern `auth/password_policy.py`'s `_common_passwords()` uses to load `auth/data/
common_passwords.txt`, so this resolves correctly from an installed wheel, not just a
source checkout.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from importlib import resources
from pathlib import Path

from ansina.errors import FixtureError
from ansina.heart.curation import CurationEvent
from ansina.heart.tick.decision import TickDecision
from ansina.heart.tick.snapshot import SnapshotItem
from ansina.heart.triage import TriageClass

_DEFAULT_RESOURCE = ("ansina.heart.eval", "data/tick_fixtures.jsonl")
_DEFAULT_TRIAGE_RESOURCE = ("ansina.heart.eval", "data/triage_fixtures.jsonl")
_DEFAULT_CURATION_RESOURCE = ("ansina.heart.eval", "data/curation_fixtures.jsonl")


@dataclass(frozen=True, slots=True)
class TickFixture:
    """One labelled tick scenario.

    `tags` marks subsets the bench report singles out — `"obviously_idle"` is the one
    issue #53's gate names explicitly (zero false act/escalate on that subset).
    """

    id: str
    expect: TickDecision
    items: tuple[SnapshotItem, ...]
    tags: frozenset[str] = field(default_factory=frozenset)
    note: str = ""


def _resolve_text(
    path: Path | None, default_resource: tuple[str, str]
) -> tuple[str, str]:
    """Read fixture-set text plus the `source` string error messages cite — shared by
    `load_fixtures`/`load_triage_fixtures`/`load_curation_fixtures`. `path` overrides
    `default_resource`'s bundled `importlib.resources` file; `None` reads the bundled
    default.

    A missing file — an explicit `path` that doesn't exist, or a bundled resource not
    yet shipped (issue #14's own `data/curation_fixtures.jsonl` is real, captured
    session data landed in a follow-up real-hardware step, not shipped with this
    code) — raises `FixtureError` rather than a raw `FileNotFoundError`/
    `ModuleNotFoundError`, so every caller (`__main__.py`'s own `except AnsinaError`)
    sees one consistent error type regardless of which of the three loaders hit it.
    """
    source = (
        str(path)
        if path is not None
        else f"{default_resource[0]}/{default_resource[1]}"
    )
    try:
        if path is None:
            package, resource_path = default_resource
            text = resources.files(package).joinpath(resource_path).read_text("utf-8")
        else:
            text = path.read_text("utf-8")
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise FixtureError(
            f"{source}: fixture file not found", details={"source": source}
        ) from exc
    return text, source


def _iter_fixture_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield `(line_number, stripped_line)` for every non-blank, non-comment line —
    the shared skip rule both fixture formats use (1-indexed, matching how a human
    would count lines in the file when an error message cites one).
    """
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            yield line_number, stripped


def _check_no_duplicate_ids(ids: Sequence[str], *, source: str) -> None:
    """The duplicate-id half of fixture-set validation — standalone because
    `load_curation_fixtures` needs it with no label enum to also be exhaustive over
    (there is no `TriageClass`-shaped label for a curation fixture), unlike
    `load_fixtures`/`load_triage_fixtures` below, which both call this from
    `_validate_fixture_set`.
    """
    seen_ids = Counter(ids)
    duplicates = sorted(id_ for id_, count in seen_ids.items() if count > 1)
    if duplicates:
        raise FixtureError(
            f"{source}: duplicate fixture id(s): {', '.join(duplicates)}",
            details={"source": source, "duplicates": duplicates},
        )


def _validate_fixture_set[LabelT: StrEnum](
    *,
    ids: Sequence[str],
    labels: Sequence[LabelT],
    label_enum: type[LabelT],
    source: str,
) -> None:
    """Shared duplicate-id / every-class-present validation — `load_fixtures`/
    `load_triage_fixtures` differ only in what one JSON object deserializes into,
    never in this.
    """
    _check_no_duplicate_ids(ids, source=source)

    present = set(labels)
    missing = sorted(member.value for member in label_enum if member not in present)
    if missing:
        raise FixtureError(
            f"{source}: no fixtures for class(es): {', '.join(missing)} — "
            "per-class recall is undefined for a class with zero examples",
            details={"source": source, "missing_classes": missing},
        )


def _parse_tick_line(line: str, *, line_number: int, source: str) -> TickFixture:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
        raise FixtureError(
            f"{source}:{line_number}: not valid JSON",
            details={"source": source, "line": line_number},
        ) from exc

    try:
        fixture_id = raw["id"]
        expect_raw = raw["expect"]
        items_raw = raw["items"]
    except KeyError as exc:
        raise FixtureError(
            f"{source}:{line_number}: missing required key {exc}",
            details={"source": source, "line": line_number},
        ) from exc

    try:
        expect = TickDecision(expect_raw)
    except ValueError as exc:
        raise FixtureError(
            f"{source}:{line_number}: {expect_raw!r} is not a valid TickDecision",
            details={"source": source, "line": line_number, "expect": expect_raw},
        ) from exc

    if not items_raw:
        raise FixtureError(
            f"{source}:{line_number}: fixture {fixture_id!r} has no items",
            details={"source": source, "line": line_number, "id": fixture_id},
        )

    items = tuple(
        SnapshotItem(
            source=item["source"], text=item["text"], priority=item.get("priority", 0)
        )
        for item in items_raw
    )

    return TickFixture(
        id=fixture_id,
        expect=expect,
        items=items,
        tags=frozenset(raw.get("tags", [])),
        note=raw.get("note", ""),
    )


def load_fixtures(path: Path | None = None) -> tuple[TickFixture, ...]:
    """Load and validate the tick fixture set.

    `path` overrides the bundled JSONL — the `__main__.py` `--fixtures` flag uses this
    to point at a smaller/different set. `None` (the default) reads the packaged
    `data/tick_fixtures.jsonl` via `importlib.resources`.

    Raises `FixtureError` on any structural problem: bad JSON, a missing key, an
    `expect` that isn't a real `TickDecision`, an empty `items` list, a duplicate `id`,
    or a `TickDecision` with zero fixtures (the bench's per-class recall is undefined
    for a class with no examples).
    """
    text, source = _resolve_text(path, _DEFAULT_RESOURCE)

    fixtures = [
        _parse_tick_line(line, line_number=line_number, source=source)
        for line_number, line in _iter_fixture_lines(text)
    ]
    if not fixtures:
        raise FixtureError(f"{source}: no fixtures found", details={"source": source})

    _validate_fixture_set(
        ids=[fixture.id for fixture in fixtures],
        labels=[fixture.expect for fixture in fixtures],
        label_enum=TickDecision,
        source=source,
    )
    return tuple(fixtures)


@dataclass(frozen=True, slots=True)
class TriageFixture:
    """One labelled inbound-request scenario. See issue #13.

    `tags` marks subsets the triage bench report singles out — mirrors `TickFixture`'s
    own `tags` field, with analogous tag names (e.g. `"obviously_trivial"` as the
    `"obviously_idle"` counterpart).
    """

    id: str
    expect: TriageClass
    request: str
    tags: frozenset[str] = field(default_factory=frozenset)
    note: str = ""


def _parse_triage_line(line: str, *, line_number: int, source: str) -> TriageFixture:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
        raise FixtureError(
            f"{source}:{line_number}: not valid JSON",
            details={"source": source, "line": line_number},
        ) from exc

    try:
        fixture_id = raw["id"]
        expect_raw = raw["expect"]
        request = raw["request"]
    except KeyError as exc:
        raise FixtureError(
            f"{source}:{line_number}: missing required key {exc}",
            details={"source": source, "line": line_number},
        ) from exc

    try:
        expect = TriageClass(expect_raw)
    except ValueError as exc:
        raise FixtureError(
            f"{source}:{line_number}: {expect_raw!r} is not a valid TriageClass",
            details={"source": source, "line": line_number, "expect": expect_raw},
        ) from exc

    if not request.strip():
        raise FixtureError(
            f"{source}:{line_number}: fixture {fixture_id!r} has an empty request",
            details={"source": source, "line": line_number, "id": fixture_id},
        )

    return TriageFixture(
        id=fixture_id,
        expect=expect,
        request=request,
        tags=frozenset(raw.get("tags", [])),
        note=raw.get("note", ""),
    )


def load_triage_fixtures(path: Path | None = None) -> tuple[TriageFixture, ...]:
    """Load and validate the triage fixture set — the `load_fixtures()` sibling issue
    #13 asks for, sharing its line-reading/validation machinery rather than
    duplicating it.

    `path` overrides the bundled JSONL. `None` (the default) reads the packaged
    `data/triage_fixtures.jsonl` via `importlib.resources`.

    Raises `FixtureError` on any structural problem: bad JSON, a missing key, an
    `expect` that isn't a real `TriageClass`, an empty `request`, a duplicate `id`, or
    a `TriageClass` with zero fixtures.
    """
    text, source = _resolve_text(path, _DEFAULT_TRIAGE_RESOURCE)

    fixtures = [
        _parse_triage_line(line, line_number=line_number, source=source)
        for line_number, line in _iter_fixture_lines(text)
    ]
    if not fixtures:
        raise FixtureError(f"{source}: no fixtures found", details={"source": source})

    _validate_fixture_set(
        ids=[fixture.id for fixture in fixtures],
        labels=[fixture.expect for fixture in fixtures],
        label_enum=TriageClass,
        source=source,
    )
    return tuple(fixtures)


@dataclass(frozen=True, slots=True)
class CurationFixture:
    """One labelled, real captured OpenCode session. See issue #14.

    `must_keep` is the human-judged minimal set of event sequence numbers essential
    to explain the session's outcome — the gate's own ground truth for recall.
    `failure_cause` is a short, human-written label the bench's keyword-match proxy
    scores a Brain answer against (see `heart.eval.curation_report`'s own docstring
    for why this is a documented, coarse proxy rather than a semantic judge).
    """

    id: str
    events: tuple[CurationEvent, ...]
    must_keep: frozenset[int]
    failure_cause: str
    tags: frozenset[str] = field(default_factory=frozenset)
    note: str = ""


def _parse_curation_line(
    line: str, *, line_number: int, source: str
) -> CurationFixture:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
        raise FixtureError(
            f"{source}:{line_number}: not valid JSON",
            details={"source": source, "line": line_number},
        ) from exc

    try:
        fixture_id = raw["id"]
        events_raw = raw["events"]
        must_keep_raw = raw["must_keep"]
        failure_cause = raw["failure_cause"]
    except KeyError as exc:
        raise FixtureError(
            f"{source}:{line_number}: missing required key {exc}",
            details={"source": source, "line": line_number},
        ) from exc

    if not events_raw:
        raise FixtureError(
            f"{source}:{line_number}: fixture {fixture_id!r} has no events",
            details={"source": source, "line": line_number, "id": fixture_id},
        )

    events = tuple(
        CurationEvent(
            sequence=event["sequence"],
            raw_type=event["raw_type"],
            payload=event["payload"],
        )
        for event in events_raw
    )
    valid_sequences = frozenset(event.sequence for event in events)
    if len(valid_sequences) != len(events):
        raise FixtureError(
            f"{source}:{line_number}: fixture {fixture_id!r} has duplicate event "
            "sequence numbers",
            details={"source": source, "line": line_number, "id": fixture_id},
        )

    must_keep = frozenset(must_keep_raw)
    if not must_keep:
        raise FixtureError(
            f"{source}:{line_number}: fixture {fixture_id!r} has an empty "
            "must_keep set",
            details={"source": source, "line": line_number, "id": fixture_id},
        )
    if not must_keep <= valid_sequences:
        invalid = sorted(must_keep - valid_sequences)
        raise FixtureError(
            f"{source}:{line_number}: fixture {fixture_id!r} has must_keep "
            f"sequence(s) not present in its own events: {invalid}",
            details={
                "source": source,
                "line": line_number,
                "id": fixture_id,
                "invalid": invalid,
            },
        )

    if not failure_cause.strip():
        raise FixtureError(
            f"{source}:{line_number}: fixture {fixture_id!r} has an empty "
            "failure_cause",
            details={"source": source, "line": line_number, "id": fixture_id},
        )

    return CurationFixture(
        id=fixture_id,
        events=events,
        must_keep=must_keep,
        failure_cause=failure_cause,
        tags=frozenset(raw.get("tags", [])),
        note=raw.get("note", ""),
    )


def load_curation_fixtures(path: Path | None = None) -> tuple[CurationFixture, ...]:
    """Load and validate the context-curation fixture set — the `load_fixtures()`/
    `load_triage_fixtures()` sibling issue #14 asks for, sharing their line-reading
    machinery and the standalone duplicate-id check
    (`_check_no_duplicate_ids`) rather than duplicating either. Unlike the other two
    loaders, this one does **not** call `_validate_fixture_set` — there is no label
    enum for a curation fixture to be exhaustive over.

    `path` overrides the bundled JSONL. `None` (the default) reads the packaged
    `data/curation_fixtures.jsonl` via `importlib.resources`.

    Raises `FixtureError` on any structural problem: bad JSON, a missing key, an
    empty `events` list, a duplicate event sequence number within one fixture, an
    empty or out-of-range `must_keep` set, an empty `failure_cause`, or a duplicate
    fixture `id`.
    """
    text, source = _resolve_text(path, _DEFAULT_CURATION_RESOURCE)

    fixtures = [
        _parse_curation_line(line, line_number=line_number, source=source)
        for line_number, line in _iter_fixture_lines(text)
    ]
    if not fixtures:
        raise FixtureError(f"{source}: no fixtures found", details={"source": source})

    _check_no_duplicate_ids([fixture.id for fixture in fixtures], source=source)
    return tuple(fixtures)
