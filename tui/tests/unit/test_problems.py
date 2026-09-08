from __future__ import annotations

from ansina_tui.exits import ExitCode
from ansina_tui.problems import exit_code_for, parse_problem


def test_recognized_code_renders_its_known_message() -> None:
    problem = parse_problem(
        {
            "type": "urn:ansina:error:ansina.forbidden",
            "title": "Forbidden",
            "status": 403,
            "detail": "no matching grant",
            "code": "ansina.forbidden",
            "request_id": "req-1",
        }
    )
    assert problem is not None
    assert problem.message == "Your role doesn't grant this action."
    assert problem.request_id == "req-1"


def test_sudo_locked_out_surfaces_retry_after_via_extra() -> None:
    problem = parse_problem(
        {
            "type": "urn:ansina:error:ansina.auth.sudo_locked_out",
            "title": "Locked Out",
            "status": 429,
            "detail": "too many attempts",
            "code": "ansina.auth.sudo_locked_out",
            "request_id": "req-2",
            "retry_after": 30,
        }
    )
    assert problem is not None
    assert problem.message == "Sudo locked out after too many failed attempts."
    assert problem.extra == {"retry_after": 30}


def test_unrecognized_code_falls_back_to_detail() -> None:
    problem = parse_problem(
        {
            "type": "urn:ansina:error:ansina.something_new",
            "title": "Something New",
            "status": 500,
            "detail": "a message the client doesn't specially recognize",
            "code": "ansina.something_new",
            "request_id": "req-3",
        }
    )
    assert problem is not None
    assert problem.message == "a message the client doesn't specially recognize"


def test_missing_detail_falls_back_to_title() -> None:
    problem = parse_problem(
        {
            "type": "urn:ansina:error:ansina.something_new",
            "title": "Something New",
            "status": 500,
            "detail": "",
            "code": "ansina.something_new",
            "request_id": None,
        }
    )
    assert problem is not None
    assert problem.message == "Something New"


def test_no_detail_no_title_falls_back_to_code_naming_message() -> None:
    problem = parse_problem({"code": "ansina.something_new"})
    assert problem is not None
    assert problem.message == "Request failed (ansina.something_new)."


def test_no_code_at_all_falls_back_to_unknown_error() -> None:
    problem = parse_problem({})
    assert problem is not None
    assert problem.message == "Request failed (unknown error)."


def test_malformed_body_is_not_a_problem() -> None:
    assert parse_problem(None) is None
    assert parse_problem("not a dict") is None
    assert parse_problem([1, 2, 3]) is None


def test_exit_code_for_maps_401_and_403_specifically() -> None:
    assert exit_code_for(401) == ExitCode.NOT_AUTHENTICATED
    assert exit_code_for(403) == ExitCode.FORBIDDEN
    assert exit_code_for(500) == ExitCode.REQUEST_FAILED
    assert exit_code_for(404) == ExitCode.REQUEST_FAILED
