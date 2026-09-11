"""Pins the secret-input contract: a token or password comes only from `--with-token`
(or an auto-detected piped stdin) or a no-echo prompt — never argv."""

from __future__ import annotations

import io

import pytest

from ansina_tui.secret_input import SecretInputError, read_password, read_token


class _FakeTty(io.StringIO):
    """A stream that reports as a real terminal, unlike a plain `io.StringIO`."""

    def isatty(self) -> bool:
        return True


def test_read_token_with_flag_reads_stdin_even_when_it_is_a_tty() -> None:
    stream = _FakeTty("tok-abc\n")
    assert read_token(with_token=True, stream=stream) == "tok-abc"


def test_read_token_auto_detects_a_piped_non_tty_stdin_without_the_flag() -> None:
    stream = io.StringIO("tok-piped\n")
    assert read_token(with_token=False, stream=stream) == "tok-piped"


def test_read_token_prompts_when_interactive_and_no_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ansina_tui.secret_input.typer.prompt", lambda *a, **k: "typed-token"
    )
    stream = _FakeTty("")
    assert read_token(with_token=False, stream=stream) == "typed-token"


def test_read_token_empty_stdin_raises() -> None:
    stream = io.StringIO("\n")
    with pytest.raises(SecretInputError) as exc_info:
        read_token(with_token=True, stream=stream)
    assert "token" in str(exc_info.value)


def test_read_token_empty_prompt_answer_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ansina_tui.secret_input.typer.prompt", lambda *a, **k: "")
    stream = _FakeTty("")
    with pytest.raises(SecretInputError):
        read_token(with_token=False, stream=stream)


def test_read_password_auto_detects_a_piped_non_tty_stdin() -> None:
    stream = io.StringIO("hunter2\n")
    assert read_password(stream=stream) == "hunter2"


def test_read_password_prompts_when_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ansina_tui.secret_input.typer.prompt", lambda *a, **k: "typed-pw"
    )
    stream = _FakeTty("")
    assert read_password(stream=stream) == "typed-pw"


def test_read_password_empty_raises() -> None:
    stream = io.StringIO("")
    with pytest.raises(SecretInputError) as exc_info:
        read_password(stream=stream)
    assert "password" in str(exc_info.value)
