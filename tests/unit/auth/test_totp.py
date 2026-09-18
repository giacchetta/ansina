from __future__ import annotations

import pytest

from ansina.auth.totp import find_valid_step, hotp, step_index, totp_code

# RFC 6238 Appendix B's own published SHA-1 test vectors: shared secret is the ASCII
# string "12345678901234567890" (20 bytes), X = 30 seconds, T0 = 0, 8-digit truncation.
# Truncation to 6 digits (this codebase's production default) is just a smaller modulus
# over the identical HMAC/offset computation, so validating the 8-digit vectors proves
# the same underlying algorithm this module implements at 6 digits.
_RFC_SECRET = b"12345678901234567890"


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
    ],
)
def test_totp_code_matches_rfc_6238_test_vectors(at: int, expected: str) -> None:
    assert totp_code(_RFC_SECRET, at=at, digits=8) == expected


def test_step_index_at_zero_is_zero() -> None:
    assert step_index(0) == 0


def test_step_index_floors_within_a_step() -> None:
    assert step_index(29) == 0
    assert step_index(30) == 1
    assert step_index(59) == 1
    assert step_index(60) == 2


def test_hotp_pads_to_the_requested_digit_count() -> None:
    code = hotp(_RFC_SECRET, 1, digits=8)
    assert len(code) == 8
    assert code.isdigit()


def test_find_valid_step_matches_the_current_step() -> None:
    secret = b"0" * 20
    at = 1_700_000_000
    code = totp_code(secret, at=at)

    matched = find_valid_step(secret, code, at=at)

    assert matched == step_index(at)


def test_find_valid_step_tolerates_one_step_of_drift_in_either_direction() -> None:
    secret = b"0" * 20
    at = 1_700_000_000
    earlier_code = totp_code(secret, at=at - 30)
    later_code = totp_code(secret, at=at + 30)

    assert find_valid_step(secret, earlier_code, at=at) == step_index(at - 30)
    assert find_valid_step(secret, later_code, at=at) == step_index(at + 30)


def test_find_valid_step_rejects_drift_beyond_the_window() -> None:
    secret = b"0" * 20
    at = 1_700_000_000
    too_old_code = totp_code(secret, at=at - 60)

    assert find_valid_step(secret, too_old_code, at=at) is None


def test_find_valid_step_rejects_a_wrong_code() -> None:
    secret = b"0" * 20
    at = 1_700_000_000
    right_code = totp_code(secret, at=at)
    last_digit = int(right_code[-1])
    wrong_code = right_code[:-1] + str((last_digit + 1) % 10)

    assert find_valid_step(secret, wrong_code, at=at) is None


def test_find_valid_step_enforces_the_anti_replay_floor() -> None:
    """The exact scenario `TotpStepUpVerifier` relies on: a code that would otherwise
    match is refused once its own step index is at or before `not_before_step`.
    """
    secret = b"0" * 20
    at = 1_700_000_000
    code = totp_code(secret, at=at)
    matched_step = step_index(at)

    # Replaying the same code again, now that it's the recorded floor, fails.
    assert find_valid_step(secret, code, at=at, not_before_step=matched_step) is None
    # An older step at or before the floor is refused even if it happens to verify.
    assert (
        find_valid_step(secret, code, at=at, not_before_step=matched_step + 10) is None
    )


def test_find_valid_step_accepts_a_step_strictly_after_the_floor() -> None:
    secret = b"0" * 20
    at = 1_700_000_000
    next_step_at = at + 30
    code = totp_code(secret, at=next_step_at)

    matched = find_valid_step(
        secret, code, at=next_step_at, not_before_step=step_index(at)
    )

    assert matched == step_index(next_step_at)
