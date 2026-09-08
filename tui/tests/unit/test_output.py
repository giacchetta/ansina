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
