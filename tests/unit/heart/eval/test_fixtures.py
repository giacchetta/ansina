from __future__ import annotations

from pathlib import Path

import pytest

from ansina.errors import FixtureError
from ansina.heart.curation import CurationEvent
from ansina.heart.eval.fixtures import (
    CurationFixture,
    FailureExtractionFixture,
    InterventionExtractionFixture,
    TickFixture,
    TitleExtractionFixture,
    TriageFixture,
    load_curation_fixtures,
    load_extraction_fixtures,
    load_fixtures,
    load_triage_fixtures,
)
from ansina.heart.extraction import FailureCause, InterventionCandidateRun
from ansina.heart.tick.decision import TickDecision
from ansina.heart.tick.snapshot import SnapshotItem
from ansina.heart.triage import TriageClass
from ansina.opencode.interventions import OpencodeAction


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


def test_load_fixtures_rejects_a_missing_custom_path(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.jsonl"

    with pytest.raises(FixtureError, match="fixture file not found"):
        load_fixtures(missing)


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


# --- load_curation_fixtures (issue #14) --------------------------------------------
#
# Unlike `load_fixtures`/`load_triage_fixtures` above, there is no "loads the bundled
# default set" test here: `data/curation_fixtures.jsonl` is real, captured OpenCode
# session data (issue #14's own AC #1 — "derived from real M9 session data, not
# synthetic"), produced in a follow-up real-hardware step, not shipped with this
# code. `_resolve_text`'s `path is None` branch is already covered by the two
# bundled-default tests above (same shared function); every test below exercises
# `load_curation_fixtures` against an explicit `tmp_path` instead.


def test_load_curation_fixtures_from_a_custom_path(tmp_path: Path) -> None:
    content = (
        '{"id": "c1", "events": [{"sequence": 0, "raw_type": "submitted", '
        '"payload": "p0"}, {"sequence": 1, "raw_type": "run_event", '
        '"payload": "p1"}], "must_keep": [0], "failure_cause": "bad assumption"}\n'
    )
    path = _write(tmp_path, content)

    fixtures = load_curation_fixtures(path)

    assert fixtures == (
        CurationFixture(
            id="c1",
            events=(
                CurationEvent(sequence=0, raw_type="submitted", payload="p0"),
                CurationEvent(sequence=1, raw_type="run_event", payload="p1"),
            ),
            must_keep=frozenset({0}),
            failure_cause="bad assumption",
        ),
    )


def test_load_curation_fixtures_skips_blank_lines_and_comments(
    tmp_path: Path,
) -> None:
    content = (
        "# a comment\n"
        "\n"
        '{"id": "a", "events": [{"sequence": 0, "raw_type": "t", "payload": "p"}], '
        '"must_keep": [0], "failure_cause": "f"}\n'
        '{"id": "b", "events": [{"sequence": 0, "raw_type": "t", "payload": "p"}], '
        '"must_keep": [0], "failure_cause": "f"}\n'
    )
    path = _write(tmp_path, content)

    fixtures = load_curation_fixtures(path)

    assert [f.id for f in fixtures] == ["a", "b"]


def test_load_curation_fixtures_parses_tags_and_note(tmp_path: Path) -> None:
    content = (
        '{"id": "a", "events": [{"sequence": 0, "raw_type": "t", "payload": "p"}], '
        '"must_keep": [0], "failure_cause": "f", "tags": ["permission_denial"], '
        '"note": "n"}\n'
        '{"id": "b", "events": [{"sequence": 0, "raw_type": "t", "payload": "p"}], '
        '"must_keep": [0], "failure_cause": "f"}\n'
    )
    path = _write(tmp_path, content)

    fixtures = load_curation_fixtures(path)

    assert fixtures[0].tags == frozenset({"permission_denial"})
    assert fixtures[0].note == "n"
    assert fixtures[1].tags == frozenset()
    assert fixtures[1].note == ""


def test_load_curation_fixtures_rejects_invalid_json(tmp_path: Path) -> None:
    path = _write(tmp_path, "not json at all\n")

    with pytest.raises(FixtureError, match="not valid JSON"):
        load_curation_fixtures(path)


def test_load_curation_fixtures_rejects_a_missing_required_key(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, '{"id": "a", "events": []}\n')

    with pytest.raises(FixtureError, match="missing required key"):
        load_curation_fixtures(path)


def test_load_curation_fixtures_rejects_empty_events(tmp_path: Path) -> None:
    content = '{"id": "a", "events": [], "must_keep": [0], "failure_cause": "f"}\n'
    path = _write(tmp_path, content)

    with pytest.raises(FixtureError, match="has no events"):
        load_curation_fixtures(path)


def test_load_curation_fixtures_rejects_duplicate_event_sequences(
    tmp_path: Path,
) -> None:
    content = (
        '{"id": "a", "events": ['
        '{"sequence": 0, "raw_type": "t", "payload": "p"}, '
        '{"sequence": 0, "raw_type": "t", "payload": "p2"}'
        '], "must_keep": [0], "failure_cause": "f"}\n'
    )
    path = _write(tmp_path, content)

    with pytest.raises(FixtureError, match="duplicate event sequence numbers"):
        load_curation_fixtures(path)


def test_load_curation_fixtures_rejects_an_empty_must_keep(tmp_path: Path) -> None:
    content = (
        '{"id": "a", "events": [{"sequence": 0, "raw_type": "t", "payload": "p"}], '
        '"must_keep": [], "failure_cause": "f"}\n'
    )
    path = _write(tmp_path, content)

    with pytest.raises(FixtureError, match="empty must_keep set"):
        load_curation_fixtures(path)


def test_load_curation_fixtures_rejects_a_must_keep_outside_valid_sequences(
    tmp_path: Path,
) -> None:
    content = (
        '{"id": "a", "events": [{"sequence": 0, "raw_type": "t", "payload": "p"}], '
        '"must_keep": [7], "failure_cause": "f"}\n'
    )
    path = _write(tmp_path, content)

    with pytest.raises(FixtureError, match="not present in its own events"):
        load_curation_fixtures(path)


def test_load_curation_fixtures_rejects_an_empty_failure_cause(
    tmp_path: Path,
) -> None:
    content = (
        '{"id": "a", "events": [{"sequence": 0, "raw_type": "t", "payload": "p"}], '
        '"must_keep": [0], "failure_cause": "  "}\n'
    )
    path = _write(tmp_path, content)

    with pytest.raises(FixtureError, match="empty failure_cause"):
        load_curation_fixtures(path)


def test_load_curation_fixtures_rejects_an_empty_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "")

    with pytest.raises(FixtureError, match="no fixtures found"):
        load_curation_fixtures(path)


def test_load_curation_fixtures_rejects_a_file_of_only_comments(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, "# just a comment\n\n")

    with pytest.raises(FixtureError, match="no fixtures found"):
        load_curation_fixtures(path)


def test_load_curation_fixtures_loads_the_bundled_default_set() -> None:
    """`data/curation_fixtures.jsonl` is real, captured OpenCode session data
    from the Mac Mini (issue #14's own AC #1 rules out a synthetic
    placeholder) — calling with no `--fixtures` override must load it cleanly.
    """
    fixtures = load_curation_fixtures()

    assert len(fixtures) >= 4
    ids = [f.id for f in fixtures]
    assert len(ids) == len(set(ids))
    for fixture in fixtures:
        assert fixture.events
        assert fixture.must_keep
        assert fixture.failure_cause.strip()


def test_load_curation_fixtures_rejects_a_missing_custom_path(
    tmp_path: Path,
) -> None:
    """Mirrors `test_load_fixtures_rejects_a_missing_custom_path` above —
    `_resolve_text` must fail cleanly (`FixtureError`), never a raw
    `FileNotFoundError`/`ModuleNotFoundError` traceback, on an explicit path
    that doesn't exist either.
    """
    missing = tmp_path / "does-not-exist.jsonl"

    with pytest.raises(FixtureError, match="fixture file not found"):
        load_curation_fixtures(missing)


def test_load_curation_fixtures_rejects_duplicate_ids(tmp_path: Path) -> None:
    content = (
        '{"id": "a", "events": [{"sequence": 0, "raw_type": "t", "payload": "p"}], '
        '"must_keep": [0], "failure_cause": "f"}\n'
        '{"id": "a", "events": [{"sequence": 0, "raw_type": "t", "payload": "p"}], '
        '"must_keep": [0], "failure_cause": "f"}\n'
    )
    path = _write(tmp_path, content)

    with pytest.raises(FixtureError, match="duplicate fixture id"):
        load_curation_fixtures(path)


# --- load_extraction_fixtures (issue #15) -------------------------------------------
#
# Mirrors `load_curation_fixtures`'s own pre-#14 shape: no "loads the bundled
# default set" test, since no real M9 run-row/event-stream data has been captured
# yet to derive a committed `data/extraction_fixtures.jsonl` from (see
# `heart.eval.fixtures`'s own module docstring) — every test below drives an
# explicit `tmp_path` fixture file, plus one test proving the missing-bundled-file
# path fails cleanly rather than with a raw traceback.

_INTERVENTION_LINE = (
    '{"job": "intervention", "id": "i1", '
    '"candidates": [{"run_id": "r1", "status": "failed", "detail": "d"}], '
    '"intake_paused": false, "recent_failed": 1, "recent_timeout": 0, '
    '"recent_permission_denied": 0, "expected_action": "retry", '
    '"expected_run_id": "r1"}'
)
_TITLE_LINE = (
    '{"job": "title", "id": "t1", "prompt": "fix the bug", '
    '"events": [{"sequence": 0, "raw_type": "text", "payload": "p"}], '
    '"expected_title": "Fix the bug"}'
)
_FAILURE_LINE = (
    '{"job": "failure_cause", "id": "f1", '
    '"events": [{"sequence": 0, "raw_type": "text", "payload": "shell denied"}], '
    '"expected_cause": "shell_denied", "expected_evidence_seq": 0}'
)


def test_load_extraction_fixtures_parses_an_intervention_fixture(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, _INTERVENTION_LINE + "\n")

    fixtures = load_extraction_fixtures(path)

    assert fixtures == (
        InterventionExtractionFixture(
            id="i1",
            candidates=(
                InterventionCandidateRun(run_id="r1", status="failed", detail="d"),
            ),
            intake_paused=False,
            recent_failed=1,
            recent_timeout=0,
            recent_permission_denied=0,
            expected_action=OpencodeAction.RETRY,
            expected_run_id="r1",
        ),
    )


def test_load_extraction_fixtures_parses_an_intervention_fixture_with_no_action(
    tmp_path: Path,
) -> None:
    """`expected_action: null` is a real, valid ground truth (no intervention
    needed) — not a missing-key error.
    """
    content = (
        '{"job": "intervention", "id": "i1", '
        '"candidates": [{"run_id": "r1", "status": "running", "detail": "d"}], '
        '"intake_paused": false, "recent_failed": 0, "recent_timeout": 0, '
        '"recent_permission_denied": 0, "expected_action": null, '
        '"expected_run_id": null}\n'
    )
    path = _write(tmp_path, content)

    fixtures = load_extraction_fixtures(path)

    assert isinstance(fixtures[0], InterventionExtractionFixture)
    assert fixtures[0].expected_action is None
    assert fixtures[0].expected_run_id is None


def test_load_extraction_fixtures_parses_a_title_fixture(tmp_path: Path) -> None:
    path = _write(tmp_path, _TITLE_LINE + "\n")

    fixtures = load_extraction_fixtures(path)

    assert fixtures == (
        TitleExtractionFixture(
            id="t1",
            prompt="fix the bug",
            events=(CurationEvent(sequence=0, raw_type="text", payload="p"),),
            expected_title="Fix the bug",
        ),
    )


def test_load_extraction_fixtures_parses_a_failure_fixture(tmp_path: Path) -> None:
    path = _write(tmp_path, _FAILURE_LINE + "\n")

    fixtures = load_extraction_fixtures(path)

    assert fixtures == (
        FailureExtractionFixture(
            id="f1",
            events=(
                CurationEvent(sequence=0, raw_type="text", payload="shell denied"),
            ),
            expected_cause=FailureCause.SHELL_DENIED,
            expected_evidence_seq=0,
        ),
    )


def test_load_extraction_fixtures_shares_one_id_namespace_across_jobs(
    tmp_path: Path,
) -> None:
    content = _INTERVENTION_LINE + "\n" + _TITLE_LINE + "\n" + _FAILURE_LINE + "\n"
    path = _write(tmp_path, content)

    fixtures = load_extraction_fixtures(path)

    assert [f.id for f in fixtures] == ["i1", "t1", "f1"]


def test_load_extraction_fixtures_skips_blank_lines_and_comments(
    tmp_path: Path,
) -> None:
    content = "# a comment\n\n" + _TITLE_LINE + "\n"
    path = _write(tmp_path, content)

    fixtures = load_extraction_fixtures(path)

    assert [f.id for f in fixtures] == ["t1"]


def test_load_extraction_fixtures_parses_tags_and_note(tmp_path: Path) -> None:
    content = _TITLE_LINE[:-1] + ', "tags": ["real"], "note": "n"}\n'
    path = _write(tmp_path, content)

    fixtures = load_extraction_fixtures(path)

    assert fixtures[0].tags == frozenset({"real"})
    assert fixtures[0].note == "n"


def test_load_extraction_fixtures_rejects_invalid_json(tmp_path: Path) -> None:
    path = _write(tmp_path, "not json at all\n")

    with pytest.raises(FixtureError, match="not valid JSON"):
        load_extraction_fixtures(path)


def test_load_extraction_fixtures_rejects_a_missing_job(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"id": "a"}\n')

    with pytest.raises(FixtureError, match="is not a valid extraction job"):
        load_extraction_fixtures(path)


def test_load_extraction_fixtures_rejects_an_unknown_job(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"job": "bogus", "id": "a"}\n')

    with pytest.raises(FixtureError, match="is not a valid extraction job"):
        load_extraction_fixtures(path)


def test_load_extraction_fixtures_rejects_a_missing_required_key(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, '{"job": "intervention", "id": "i1"}\n')

    with pytest.raises(FixtureError, match="missing required key"):
        load_extraction_fixtures(path)


def test_load_extraction_fixtures_rejects_a_missing_required_key_in_a_title(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, '{"job": "title", "id": "t1"}\n')

    with pytest.raises(FixtureError, match="missing required key"):
        load_extraction_fixtures(path)


def test_load_extraction_fixtures_rejects_a_missing_required_key_in_a_failure(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, '{"job": "failure_cause", "id": "f1"}\n')

    with pytest.raises(FixtureError, match="missing required key"):
        load_extraction_fixtures(path)


def test_load_extraction_fixtures_rejects_empty_candidates(tmp_path: Path) -> None:
    content = (
        '{"job": "intervention", "id": "i1", "candidates": [], '
        '"intake_paused": false, "recent_failed": 0, "recent_timeout": 0, '
        '"recent_permission_denied": 0, "expected_action": null, '
        '"expected_run_id": null}\n'
    )
    path = _write(tmp_path, content)

    with pytest.raises(FixtureError, match="has no candidates"):
        load_extraction_fixtures(path)


def test_load_extraction_fixtures_rejects_an_invalid_expected_action(
    tmp_path: Path,
) -> None:
    content = (
        '{"job": "intervention", "id": "i1", '
        '"candidates": [{"run_id": "r1", "status": "failed", "detail": "d"}], '
        '"intake_paused": false, "recent_failed": 0, "recent_timeout": 0, '
        '"recent_permission_denied": 0, "expected_action": "bogus", '
        '"expected_run_id": null}\n'
    )
    path = _write(tmp_path, content)

    with pytest.raises(FixtureError, match="not a valid OpencodeAction"):
        load_extraction_fixtures(path)


def test_load_extraction_fixtures_rejects_an_expected_run_id_outside_candidates(
    tmp_path: Path,
) -> None:
    content = (
        '{"job": "intervention", "id": "i1", '
        '"candidates": [{"run_id": "r1", "status": "failed", "detail": "d"}], '
        '"intake_paused": false, "recent_failed": 0, "recent_timeout": 0, '
        '"recent_permission_denied": 0, "expected_action": "retry", '
        '"expected_run_id": "nope"}\n'
    )
    path = _write(tmp_path, content)

    with pytest.raises(FixtureError, match="not present among its own candidates"):
        load_extraction_fixtures(path)


def test_load_extraction_fixtures_rejects_an_empty_prompt(tmp_path: Path) -> None:
    content = (
        '{"job": "title", "id": "t1", "prompt": "  ", '
        '"events": [{"sequence": 0, "raw_type": "t", "payload": "p"}], '
        '"expected_title": "a title"}\n'
    )
    path = _write(tmp_path, content)

    with pytest.raises(FixtureError, match="empty prompt"):
        load_extraction_fixtures(path)


def test_load_extraction_fixtures_rejects_an_empty_expected_title(
    tmp_path: Path,
) -> None:
    content = (
        '{"job": "title", "id": "t1", "prompt": "do it", '
        '"events": [{"sequence": 0, "raw_type": "t", "payload": "p"}], '
        '"expected_title": "   "}\n'
    )
    path = _write(tmp_path, content)

    with pytest.raises(FixtureError, match="empty expected_title"):
        load_extraction_fixtures(path)


def test_load_extraction_fixtures_rejects_duplicate_event_sequences_in_a_title(
    tmp_path: Path,
) -> None:
    content = (
        '{"job": "title", "id": "t1", "prompt": "do it", '
        '"events": ['
        '{"sequence": 0, "raw_type": "t", "payload": "p"}, '
        '{"sequence": 0, "raw_type": "t", "payload": "p2"}'
        '], "expected_title": "a title"}\n'
    )
    path = _write(tmp_path, content)

    with pytest.raises(FixtureError, match="duplicate event sequence numbers"):
        load_extraction_fixtures(path)


def test_load_extraction_fixtures_rejects_an_invalid_expected_cause(
    tmp_path: Path,
) -> None:
    content = (
        '{"job": "failure_cause", "id": "f1", '
        '"events": [{"sequence": 0, "raw_type": "t", "payload": "p"}], '
        '"expected_cause": "bogus", "expected_evidence_seq": 0}\n'
    )
    path = _write(tmp_path, content)

    with pytest.raises(FixtureError, match="not a valid FailureCause"):
        load_extraction_fixtures(path)


def test_load_extraction_fixtures_rejects_an_evidence_seq_outside_events(
    tmp_path: Path,
) -> None:
    content = (
        '{"job": "failure_cause", "id": "f1", '
        '"events": [{"sequence": 0, "raw_type": "t", "payload": "p"}], '
        '"expected_cause": "shell_denied", "expected_evidence_seq": 7}\n'
    )
    path = _write(tmp_path, content)

    with pytest.raises(FixtureError, match="not present among its own events"):
        load_extraction_fixtures(path)


def test_load_extraction_fixtures_rejects_empty_events_in_a_failure_fixture(
    tmp_path: Path,
) -> None:
    content = (
        '{"job": "failure_cause", "id": "f1", "events": [], '
        '"expected_cause": "shell_denied", "expected_evidence_seq": 0}\n'
    )
    path = _write(tmp_path, content)

    with pytest.raises(FixtureError, match="has no events"):
        load_extraction_fixtures(path)


def test_load_extraction_fixtures_rejects_an_empty_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "")

    with pytest.raises(FixtureError, match="no fixtures found"):
        load_extraction_fixtures(path)


def test_load_extraction_fixtures_rejects_a_file_of_only_comments(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, "# just a comment\n\n")

    with pytest.raises(FixtureError, match="no fixtures found"):
        load_extraction_fixtures(path)


def test_load_extraction_fixtures_rejects_a_missing_custom_path(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "does-not-exist.jsonl"

    with pytest.raises(FixtureError, match="fixture file not found"):
        load_extraction_fixtures(missing)


def test_load_extraction_fixtures_rejects_a_missing_bundled_file() -> None:
    """`data/extraction_fixtures.jsonl` is deliberately not shipped yet (see
    `heart.eval.fixtures`'s own module docstring) — calling with no override must
    fail cleanly (`FixtureError`), never a raw traceback, until real M9 data lands
    it (the same gap `load_curation_fixtures` had before issue #14's own capture).
    """
    with pytest.raises(FixtureError, match="fixture file not found"):
        load_extraction_fixtures()


def test_load_extraction_fixtures_rejects_duplicate_ids(tmp_path: Path) -> None:
    content = _TITLE_LINE + "\n" + _TITLE_LINE + "\n"
    path = _write(tmp_path, content)

    with pytest.raises(FixtureError, match="duplicate fixture id"):
        load_extraction_fixtures(path)
