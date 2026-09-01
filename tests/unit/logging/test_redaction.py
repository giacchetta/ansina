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


def test_password_hash_key_value_is_redacted() -> None:
    result = redact('password_hash="some-opaque-hash-value" logged by accident')
    assert "some-opaque-hash-value" not in result


def test_token_hash_key_value_is_redacted() -> None:
    result = redact("token_hash=deadbeefcafef00dbaadf00d1234")
    assert "deadbeefcafef00dbaadf00d1234" not in result


def test_bare_hash_key_value_is_redacted() -> None:
    result = redact("hash=deadbeefcafef00dbaadf00d1234")
    assert "deadbeefcafef00dbaadf00d1234" not in result


def test_salt_key_value_is_redacted() -> None:
    result = redact("salt=0123456789abcdef0123456789abcdef")
    assert "0123456789abcdef0123456789abcdef" not in result


def test_argon2_phc_hash_is_redacted_wherever_it_appears() -> None:
    """Not just after a recognized key name — issue #24's redaction test covers a raw
    PHC-format hash landing in a log line via an exception message or row dump too.
    """
    phc = (
        "$argon2id$v=19$m=65536,t=3,p=4$"
        "n/uwm3486M9Vu0z3xpnCdw$d59ni8dENGl/jazIaIt0uyhZ9vVjcXEltXQOtFFcS10"
    )
    result = redact(f"unexpected row dump: {phc}")
    assert phc not in result
    assert REDACTED in result
