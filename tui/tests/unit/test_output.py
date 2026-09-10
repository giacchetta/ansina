"""Pins the output-discipline contract: with `--json`, stdout carries JSON and
nothing else; every diagnostic goes to stderr, `--json` or not."""

from __future__ import annotations

import json

import pytest

from ansina_tui.output import Emitter


def test_json_mode_stdout_is_exactly_the_json_payload(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emitter = Emitter(json_mode=True)

    emitter.json({"host": "http://x", "healthy": True})

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"host": "http://x", "healthy": True}
    assert captured.err == ""


def test_json_mode_suppresses_rows_and_lines(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emitter = Emitter(json_mode=True)

    emitter.rows([("host", "http://x")])
    emitter.line("some human line")

    captured = capsys.readouterr()
    assert captured.out == ""


def test_json_mode_error_still_goes_to_stderr_not_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emitter = Emitter(json_mode=True)

    emitter.error("boom")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "boom" in captured.err


def test_human_mode_rows_render_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    emitter = Emitter(json_mode=False)

    emitter.rows([("host", "http://127.0.0.1:8000"), ("health", "ok")])

    captured = capsys.readouterr()
    assert "http://127.0.0.1:8000" in captured.out
    assert "health" in captured.out
    assert captured.err == ""


def test_human_mode_line_renders_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    emitter = Emitter(json_mode=False)

    emitter.line("hello")

    captured = capsys.readouterr()
    assert "hello" in captured.out


def test_human_mode_error_goes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    emitter = Emitter(json_mode=False)

    emitter.error("something failed")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "something failed" in captured.err


def test_warn_always_goes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    emitter = Emitter(json_mode=True)

    emitter.warn("careful")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "careful" in captured.err


def test_debug_is_silent_unless_verbose(capsys: pytest.CaptureFixture[str]) -> None:
    quiet = Emitter(verbose=False)
    quiet.debug("request id: abc")
    assert capsys.readouterr().err == ""

    loud = Emitter(verbose=True)
    loud.debug("request id: abc")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "request id: abc" in captured.err


def test_table_renders_columns_and_rows(capsys: pytest.CaptureFixture[str]) -> None:
    emitter = Emitter(json_mode=False)

    emitter.table(["id", "label"], [["tok-1", "laptop"], ["tok-2", "(none)"]])

    captured = capsys.readouterr()
    assert "tok-1" in captured.out
    assert "laptop" in captured.out
    assert "tok-2" in captured.out


def test_table_suppressed_in_json_mode(capsys: pytest.CaptureFixture[str]) -> None:
    emitter = Emitter(json_mode=True)

    emitter.table(["id"], [["tok-1"]])

    assert capsys.readouterr().out == ""


def test_body_writes_verbatim_and_is_not_suppressed_by_json_mode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emitter = Emitter(json_mode=True)

    emitter.body('{"a":1}')

    captured = capsys.readouterr()
    assert captured.out == '{"a":1}\n'
    assert captured.err == ""


def test_body_writes_verbatim_in_human_mode_too(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emitter = Emitter(json_mode=False)

    emitter.body("plain text")

    assert capsys.readouterr().out == "plain text\n"


def test_body_does_not_duplicate_a_trailing_newline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emitter = Emitter()

    emitter.body("already has one\n")

    assert capsys.readouterr().out == "already has one\n"


def test_body_writes_nothing_for_an_empty_string(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emitter = Emitter()

    emitter.body("")

    assert capsys.readouterr().out == ""
