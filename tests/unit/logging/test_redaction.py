from __future__ import annotations

from ansina.logging.redaction import REDACTED, redact, register_secret


def test_bearer_token_is_redacted() -> None:
    result = redact("Authorization: Bearer abc123def456")
    assert "abc123def456" not in result
    assert result == "Authorization: Bearer ***"


def test_key_value_assignment_is_redacted() -> None:
    result = redact("token=abcdef1234567890 leaked")
    assert "abcdef1234567890" not in result
    assert result == "token=*** leaked"


def test_quoted_key_value_assignment_is_redacted() -> None:
    result = redact('api_key: "sk-abcdefghijklmnopqrstuvwx"')
    assert "sk-abcdefghijklmnopqrstuvwx" not in result


def test_vendor_prefixed_token_is_redacted() -> None:
    result = redact("leaked ghp_abcdefghijklmnopqrstuvwx1234 in a message")
    assert "ghp_abcdefghijklmnopqrstuvwx1234" not in result


def test_registered_secret_is_redacted_verbatim() -> None:
    register_secret("s3cr3t-literal-value")
    result = redact("the configured value was s3cr3t-literal-value in this line")
    assert "s3cr3t-literal-value" not in result
    assert REDACTED in result


def test_short_registered_value_is_ignored() -> None:
    register_secret("abc")  # shorter than the minimum secret length
    result = redact("abc appears here and here: abc")
    assert result == "abc appears here and here: abc"


def test_none_registered_value_is_a_no_op() -> None:
    register_secret(None)  # must not raise


def test_ordinary_message_is_not_over_redacted() -> None:
    message = "user requested export, count=42, status=ok"
    assert redact(message) == message


def test_ordinary_key_equals_word_survives() -> None:
    message = "normal message key=value no secret here"
    assert redact(message) == message
