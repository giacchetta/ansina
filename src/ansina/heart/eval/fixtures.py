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

Both loaders read via `importlib.resources`, not `Path(__file__)` — the same pattern
`auth/password_policy.py`'s `_common_passwords()` uses to load `auth/data/
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
from ansina.heart.tick.decision import TickDecision
from ansina.heart.tick.snapshot import SnapshotItem
from ansina.heart.triage import TriageClass

_DEFAULT_RESOURCE = ("ansina.heart.eval", "data/tick_fixtures.jsonl")
_DEFAULT_TRIAGE_RESOURCE = ("ansina.heart.eval", "data/triage_fixtures.jsonl")


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
    `load_fixtures`/`load_triage_fixtures`. `path` overrides `default_resource`'s
    bundled `importlib.resources` file; `None` reads the bundled default.
    """
    if path is None:
        package, resource_path = default_resource
        text = resources.files(package).joinpath(resource_path).read_text("utf-8")
        return text, f"{package}/{resource_path}"
    return path.read_text("utf-8"), str(path)


def _iter_fixture_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield `(line_number, stripped_line)` for every non-blank, non-comment line —
    the shared skip rule both fixture formats use (1-indexed, matching how a human
    would count lines in the file when an error message cites one).
    """
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            yield line_number, stripped


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
    seen_ids = Counter(ids)
    duplicates = sorted(id_ for id_, count in seen_ids.items() if count > 1)
    if duplicates:
        raise FixtureError(
            f"{source}: duplicate fixture id(s): {', '.join(duplicates)}",
            details={"source": source, "duplicates": duplicates},
        )

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
