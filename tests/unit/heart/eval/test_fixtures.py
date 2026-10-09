from __future__ import annotations

from pathlib import Path

import pytest

from ansina.errors import FixtureError
from ansina.heart.eval.fixtures import (
    TickFixture,
    TriageFixture,
    load_fixtures,
    load_triage_fixtures,
)
from ansina.heart.tick.decision import TickDecision
from ansina.heart.tick.snapshot import SnapshotItem
from ansina.heart.triage import TriageClass


def test_load_fixtures_loads_the_bundled_default_set() -> None:
    fixtures = load_fixtures()

    assert len(fixtures) >= 20
    ids = [f.id for f in fixtures]
    assert len(ids) == len(set(ids))
    for expect in TickDecision:
        assert any(f.expect == expect for f in fixtures)
    for fixture in fixtures:
        assert fixture.items
        assert all(isinstance(item, SnapshotItem) for item in fixture.items)


def test_load_fixtures_has_the_obviously_idle_tag_only_on_idle_fixtures() -> None:
    fixtures = load_fixtures()

    for fixture in fixtures:
        if "obviously_idle" in fixture.tags:
            assert fixture.expect == TickDecision.IDLE


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "fixtures.jsonl"
    path.write_text(content)
    return path


def test_load_fixtures_from_a_custom_path(tmp_path: Path) -> None:
    content = (
        '{"id": "a", "expect": "idle", "items": [{"source": "s", "text": "t"}]}\n'
        '{"id": "b", "expect": "act", "items": [{"source": "s", "text": "t"}]}\n'
        '{"id": "c", "expect": "escalate", "items": [{"source": "s", "text": "t"}]}\n'
    )
    path = _write(tmp_path, content)

    fixtures = load_fixtures(path)

    assert fixtures == (
        TickFixture(
            id="a",
            expect=TickDecision.IDLE,
            items=(SnapshotItem(source="s", text="t"),),
        ),
        TickFixture(
            id="b",
            expect=TickDecision.ACT,
            items=(SnapshotItem(source="s", text="t"),),
        ),
        TickFixture(
            id="c",
            expect=TickDecision.ESCALATE,
            items=(SnapshotItem(source="s", text="t"),),
        ),
    )


def test_load_fixtures_skips_blank_lines_and_comments(tmp_path: Path) -> None:
    content = (
        "# a comment\n"
        "\n"
        '{"id": "a", "expect": "idle", "items": [{"source": "s", "text": "t"}]}\n'
        '{"id": "b", "expect": "act", "items": [{"source": "s", "text": "t"}]}\n'
        '{"id": "c", "expect": "escalate", "items": [{"source": "s", "text": "t"}]}\n'
    )
    path = _write(tmp_path, content)

    fixtures = load_fixtures(path)

    assert [f.id for f in fixtures] == ["a", "b", "c"]


def test_load_fixtures_parses_tags_and_note(tmp_path: Path) -> None:
    content = (
        '{"id": "a", "expect": "idle", "tags": ["obviously_idle"], "note": "n", '
        '"items": [{"source": "s", "text": "t", "priority": 3}]}\n'
        '{"id": "b", "expect": "act", "items": [{"source": "s", "text": "t"}]}\n'
        '{"id": "c", "expect": "escalate", "items": [{"source": "s", "text": "t"}]}\n'
    )
    path = _write(tmp_path, content)

    fixtures = load_fixtures(path)

    assert fixtures[0].tags == frozenset({"obviously_idle"})
    assert fixtures[0].note == "n"
    assert fixtures[0].items[0].priority == 3
    assert fixtures[1].tags == frozenset()
    assert fixtures[1].note == ""


def test_load_fixtures_rejects_invalid_json(tmp_path: Path) -> None:
    path = _write(tmp_path, "not json at all\n")

    with pytest.raises(FixtureError, match="not valid JSON"):
        load_fixtures(path)


def test_load_fixtures_rejects_a_missing_required_key(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"id": "a", "expect": "idle"}\n')

    with pytest.raises(FixtureError, match="missing required key"):
        load_fixtures(path)


def test_load_fixtures_rejects_an_invalid_expect(tmp_path: Path) -> None:
    content = '{"id": "a", "expect": "nope", "items": [{"source": "s", "text": "t"}]}\n'
    path = _write(tmp_path, content)

    with pytest.raises(FixtureError, match="not a valid TickDecision"):
        load_fixtures(path)


def test_load_fixtures_rejects_empty_items(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"id": "a", "expect": "idle", "items": []}\n')

    with pytest.raises(FixtureError, match="has no items"):
        load_fixtures(path)


def test_load_fixtures_rejects_an_empty_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "")

    with pytest.raises(FixtureError, match="no fixtures found"):
        load_fixtures(path)


def test_load_fixtures_rejects_a_file_of_only_comments(tmp_path: Path) -> None:
    path = _write(tmp_path, "# just a comment\n\n")

    with pytest.raises(FixtureError, match="no fixtures found"):
        load_fixtures(path)


def test_load_fixtures_rejects_duplicate_ids(tmp_path: Path) -> None:
    content = (
        '{"id": "a", "expect": "idle", "items": [{"source": "s", "text": "t"}]}\n'
        '{"id": "a", "expect": "act", "items": [{"source": "s", "text": "t"}]}\n'
        '{"id": "b", "expect": "escalate", "items": [{"source": "s", "text": "t"}]}\n'
    )
    path = _write(tmp_path, content)

    with pytest.raises(FixtureError, match="duplicate fixture id"):
        load_fixtures(path)


def test_load_fixtures_rejects_a_missing_class(tmp_path: Path) -> None:
    content = (
        '{"id": "a", "expect": "idle", "items": [{"source": "s", "text": "t"}]}\n'
        '{"id": "b", "expect": "act", "items": [{"source": "s", "text": "t"}]}\n'
    )
    path = _write(tmp_path, content)

    with pytest.raises(FixtureError, match="no fixtures for class"):
        load_fixtures(path)


# --- load_triage_fixtures (issue #13) ----------------------------------------------


def test_load_triage_fixtures_loads_the_bundled_default_set() -> None:
    fixtures = load_triage_fixtures()

    assert len(fixtures) >= 20
    ids = [f.id for f in fixtures]
    assert len(ids) == len(set(ids))
    for expect in TriageClass:
        assert any(f.expect == expect for f in fixtures)
    for fixture in fixtures:
        assert fixture.request.strip()
        assert fixture.note, f"{fixture.id} has no labelling rationale"


def test_load_triage_fixtures_from_a_custom_path(tmp_path: Path) -> None:
    content = (
        '{"id": "a", "expect": "trivial", "request": "what is 2+2?"}\n'
        '{"id": "b", "expect": "tool-only", "request": "pause the tick loop"}\n'
        '{"id": "c", "expect": "complex", "request": "design a migration plan"}\n'
    )
    path = _write(tmp_path, content)

    fixtures = load_triage_fixtures(path)

    assert fixtures == (
        TriageFixture(id="a", expect=TriageClass.TRIVIAL, request="what is 2+2?"),
        TriageFixture(
            id="b", expect=TriageClass.TOOL_ONLY, request="pause the tick loop"
        ),
        TriageFixture(
            id="c", expect=TriageClass.COMPLEX, request="design a migration plan"
        ),
    )


def test_load_triage_fixtures_skips_blank_lines_and_comments(tmp_path: Path) -> None:
    content = (
        "# a comment\n"
        "\n"
        '{"id": "a", "expect": "trivial", "request": "r"}\n'
        '{"id": "b", "expect": "tool-only", "request": "r"}\n'
        '{"id": "c", "expect": "complex", "request": "r"}\n'
    )
    path = _write(tmp_path, content)

    fixtures = load_triage_fixtures(path)

    assert [f.id for f in fixtures] == ["a", "b", "c"]


def test_load_triage_fixtures_parses_tags_and_note(tmp_path: Path) -> None:
    content = (
        '{"id": "a", "expect": "trivial", "tags": ["obviously_trivial"], '
        '"note": "n", "request": "r"}\n'
        '{"id": "b", "expect": "tool-only", "request": "r"}\n'
        '{"id": "c", "expect": "complex", "request": "r"}\n'
    )
    path = _write(tmp_path, content)

    fixtures = load_triage_fixtures(path)

    assert fixtures[0].tags == frozenset({"obviously_trivial"})
    assert fixtures[0].note == "n"
    assert fixtures[1].tags == frozenset()
    assert fixtures[1].note == ""


def test_load_triage_fixtures_rejects_invalid_json(tmp_path: Path) -> None:
    path = _write(tmp_path, "not json at all\n")

    with pytest.raises(FixtureError, match="not valid JSON"):
        load_triage_fixtures(path)


def test_load_triage_fixtures_rejects_a_missing_required_key(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"id": "a", "expect": "trivial"}\n')

    with pytest.raises(FixtureError, match="missing required key"):
        load_triage_fixtures(path)


def test_load_triage_fixtures_rejects_an_invalid_expect(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"id": "a", "expect": "nope", "request": "r"}\n')

    with pytest.raises(FixtureError, match="not a valid TriageClass"):
        load_triage_fixtures(path)


def test_load_triage_fixtures_rejects_an_empty_request(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"id": "a", "expect": "trivial", "request": "  "}\n')

    with pytest.raises(FixtureError, match="has an empty request"):
        load_triage_fixtures(path)


def test_load_triage_fixtures_rejects_an_empty_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "")

    with pytest.raises(FixtureError, match="no fixtures found"):
        load_triage_fixtures(path)


def test_load_triage_fixtures_rejects_a_file_of_only_comments(tmp_path: Path) -> None:
    path = _write(tmp_path, "# just a comment\n\n")

    with pytest.raises(FixtureError, match="no fixtures found"):
        load_triage_fixtures(path)


def test_load_triage_fixtures_rejects_duplicate_ids(tmp_path: Path) -> None:
    content = (
        '{"id": "a", "expect": "trivial", "request": "r"}\n'
        '{"id": "a", "expect": "tool-only", "request": "r"}\n'
        '{"id": "b", "expect": "complex", "request": "r"}\n'
    )
    path = _write(tmp_path, content)

    with pytest.raises(FixtureError, match="duplicate fixture id"):
        load_triage_fixtures(path)


def test_load_triage_fixtures_rejects_a_missing_class(tmp_path: Path) -> None:
    content = (
        '{"id": "a", "expect": "trivial", "request": "r"}\n'
        '{"id": "b", "expect": "tool-only", "request": "r"}\n'
    )
    path = _write(tmp_path, content)

    with pytest.raises(FixtureError, match="no fixtures for class"):
        load_triage_fixtures(path)
