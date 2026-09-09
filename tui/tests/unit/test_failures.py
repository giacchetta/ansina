"""Pins `failures.report`'s rendering contract: the recognized message (or a
per-call override), the request id only under `--verbose`, and a rendered
Retry-After figure when the daemon sent one (issue #26's sudo lockout)."""

from __future__ import annotations

import pytest

from ansina_tui.client import ApiResponse
from ansina_tui.exits import ExitCode
from ansina_tui.failures import report
from ansina_tui.output import Emitter
from ansina_tui.problems import parse_problem


def _response(status_code: int, body: object | None) -> ApiResponse:
    return ApiResponse(
        status_code=status_code,
        request_id="req-xyz",
        json_body=body,
        problem=parse_problem(body),
    )


def test_reports_known_message_and_maps_exit_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emitter = Emitter()
    response = _response(403, {"code": "ansina.forbidden"})

    exit_code = report(emitter, response)

    assert exit_code == ExitCode.FORBIDDEN
    assert "Your role doesn't grant this action." in capsys.readouterr().err


def test_message_override_replaces_the_recognized_table_entry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emitter = Emitter()
    response = _response(401, {"code": "ansina.unauthorized"})

    report(emitter, response, messages={"ansina.unauthorized": "Custom message."})

    captured = capsys.readouterr().err
    assert "Custom message." in captured
    assert "Run `auth login`" not in captured


def test_request_id_shown_only_when_verbose(
    capsys: pytest.CaptureFixture[str],
) -> None:
    quiet = Emitter(verbose=False)
    report(quiet, _response(500, {"code": "ansina.internal_error"}))
    assert "req-xyz" not in capsys.readouterr().err

    loud = Emitter(verbose=True)
    report(loud, _response(500, {"code": "ansina.internal_error"}))
    assert "req-xyz" in capsys.readouterr().err


def test_retry_after_rendered_from_problem_extra(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emitter = Emitter()
    response = _response(
        429, {"code": "ansina.auth.sudo_locked_out", "retry_after_seconds": 42}
    )

    exit_code = report(emitter, response)

    assert exit_code == ExitCode.REQUEST_FAILED
    assert "42" in capsys.readouterr().err


def test_no_retry_after_when_extra_lacks_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emitter = Emitter()
    response = _response(429, {"code": "ansina.auth.sudo_locked_out"})

    report(emitter, response)

    assert "Retry after" not in capsys.readouterr().err


def test_no_problem_body_falls_back_to_a_generic_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emitter = Emitter()
    response = _response(502, None)

    exit_code = report(emitter, response)

    assert exit_code == ExitCode.REQUEST_FAILED
    assert "HTTP 502" in capsys.readouterr().err
