"""Labelled tick fixtures for the bench harness. See issue #53.

A fixture is one hand-labelled tick scenario: the `SnapshotItem`s a real
`StateSnapshotSource` set would have contributed, and the `TickDecision` a correct
Heart should reach given them. `load_fixtures()` deserializes the bundled JSONL set
into real `heart.tick.snapshot.SnapshotItem`s, so `heart.eval.runner` renders every
fixture through the actual shipped `build_prompt` — never a parallel copy of it.

Loaded via `importlib.resources`, not `Path(__file__)` — the same pattern
`auth/password_policy.py`'s `_common_passwords()` uses to load `auth/data/
common_passwords.txt`, so this resolves correctly from an installed wheel, not just a
source checkout.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from ansina.errors import FixtureError
from ansina.heart.tick.decision import TickDecision
from ansina.heart.tick.snapshot import SnapshotItem

_DEFAULT_RESOURCE = ("ansina.heart.eval", "data/tick_fixtures.jsonl")


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


def _parse_line(line: str, *, line_number: int, source: str) -> TickFixture:
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
    if path is None:
        package, resource_path = _DEFAULT_RESOURCE
        text = resources.files(package).joinpath(resource_path).read_text("utf-8")
        source = f"{package}/{resource_path}"
    else:
        text = path.read_text("utf-8")
        source = str(path)

    fixtures: list[TickFixture] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fixtures.append(_parse_line(stripped, line_number=line_number, source=source))

    if not fixtures:
        raise FixtureError(f"{source}: no fixtures found", details={"source": source})

    _validate_set(fixtures, source=source)
    return tuple(fixtures)


def _validate_set(fixtures: Sequence[TickFixture], *, source: str) -> None:
    seen_ids = Counter(fixture.id for fixture in fixtures)
    duplicates = sorted(id_ for id_, count in seen_ids.items() if count > 1)
    if duplicates:
        raise FixtureError(
            f"{source}: duplicate fixture id(s): {', '.join(duplicates)}",
            details={"source": source, "duplicates": duplicates},
        )

    present = {fixture.expect for fixture in fixtures}
    missing = sorted(d.value for d in TickDecision if d not in present)
    if missing:
        raise FixtureError(
            f"{source}: no fixtures for class(es): {', '.join(missing)} — "
            "per-class recall is undefined for a class with zero examples",
            details={"source": source, "missing_classes": missing},
        )
