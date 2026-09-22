"""Unit tests for `ansina.auth.password_policy` (issue #48)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ansina.auth.password_policy import (
    WeakPasswordError,
    _common_passwords,
    assert_password_acceptable,
)
from ansina.config import load_settings

# `load_settings()`'s own defaults: min_length=12, max_length=1024, reject_common=True.
# Every "accepted" case below must clear all four rules simultaneously.
_ACCEPTABLE = "a truly excellent passphrase"

# A real entry from the bundled list that's also long enough (>= 12) to clear the
# *default* min_length on its own — the common-list-specific tests below use this
# rather than an arbitrary `_common_passwords()` element, since most of the list's
# 8-11 char entries would otherwise be refused for min_length first, never reaching
# the rule under test.
_LONG_COMMON_PASSWORD = "motherfucker"


def test_accepts_a_password_that_clears_every_rule(
    clean_env: None, tmp_cwd: Path
) -> None:
    settings = load_settings()

    assert_password_acceptable(_ACCEPTABLE, username="erin", settings=settings)


def test_rejects_a_password_shorter_than_min_length(
    clean_env: None, tmp_cwd: Path
) -> None:
    settings = load_settings()

    with pytest.raises(WeakPasswordError) as excinfo:
        assert_password_acceptable("short1x", username="erin", settings=settings)

    assert excinfo.value.details["rule"] == "min_length"
    assert excinfo.value.details["min_length"] == settings.security.password.min_length
    # The rejected value is never echoed back — see the module docstring.
    assert "short1x" not in str(excinfo.value)
    assert "short1x" not in repr(excinfo.value.details)


def test_min_length_is_configurable(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_SECURITY__PASSWORD__MIN_LENGTH", "8")
    settings = load_settings()

    # 8 characters now clears the (lowered) floor.
    assert_password_acceptable("xk7!mQ2z", username="erin", settings=settings)


def test_rejects_a_password_longer_than_max_length(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_SECURITY__PASSWORD__MAX_LENGTH", "20")
    settings = load_settings()

    with pytest.raises(WeakPasswordError) as excinfo:
        assert_password_acceptable(
            "this passphrase is deliberately far too long",
            username="erin",
            settings=settings,
        )

    assert excinfo.value.details["rule"] == "max_length"
    assert excinfo.value.details["max_length"] == 20


def test_rejects_a_password_containing_the_username(
    clean_env: None, tmp_cwd: Path
) -> None:
    settings = load_settings()

    with pytest.raises(WeakPasswordError) as excinfo:
        assert_password_acceptable(
            "erin is the best erin around", username="erin", settings=settings
        )

    assert excinfo.value.details["rule"] == "contains_username"


def test_username_containment_check_is_case_insensitive(
    clean_env: None, tmp_cwd: Path
) -> None:
    settings = load_settings()

    with pytest.raises(WeakPasswordError) as excinfo:
        assert_password_acceptable(
            "ErInErInErInErIn12345", username="erin", settings=settings
        )

    assert excinfo.value.details["rule"] == "contains_username"


def test_empty_username_never_matches_containment(
    clean_env: None, tmp_cwd: Path
) -> None:
    """An empty `username` would make `"" in folded` trivially `True` for every
    password if checked unconditionally — guarded against explicitly.
    """
    settings = load_settings()

    assert_password_acceptable(_ACCEPTABLE, username="", settings=settings)


def test_rejects_a_password_on_the_common_list(clean_env: None, tmp_cwd: Path) -> None:
    settings = load_settings()
    assert _LONG_COMMON_PASSWORD in _common_passwords()  # the fixture is real

    with pytest.raises(WeakPasswordError) as excinfo:
        assert_password_acceptable(
            _LONG_COMMON_PASSWORD, username="erin", settings=settings
        )

    assert excinfo.value.details["rule"] == "common_password"


def test_common_list_check_is_case_insensitive(clean_env: None, tmp_cwd: Path) -> None:
    settings = load_settings()

    with pytest.raises(WeakPasswordError) as excinfo:
        assert_password_acceptable(
            _LONG_COMMON_PASSWORD.upper(), username="erin", settings=settings
        )

    assert excinfo.value.details["rule"] == "common_password"


def test_reject_common_false_bypasses_only_the_common_list_rule(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_SECURITY__PASSWORD__REJECT_COMMON", "false")
    settings = load_settings()

    # No longer rejected for being common...
    assert_password_acceptable(
        _LONG_COMMON_PASSWORD, username="erin", settings=settings
    )

    # ...but every other rule still applies.
    with pytest.raises(WeakPasswordError) as excinfo:
        assert_password_acceptable("short1x", username="erin", settings=settings)
    assert excinfo.value.details["rule"] == "min_length"


def test_rule_precedence_min_length_checked_before_max_length(
    clean_env: None, tmp_cwd: Path
) -> None:
    """A password can't simultaneously fail both — this just documents which check
    runs first by construction (min_length, then max_length, then username, then
    common list), matching the module docstring's stated order.
    """
    settings = load_settings()

    with pytest.raises(WeakPasswordError) as excinfo:
        assert_password_acceptable("x", username="erin", settings=settings)

    assert excinfo.value.details["rule"] == "min_length"


def test_rule_precedence_username_checked_before_common_list(
    clean_env: None, tmp_cwd: Path
) -> None:
    """A password containing the username, long enough to also risk being on the
    common list, is refused for containing the username first.
    """
    settings = load_settings()

    with pytest.raises(WeakPasswordError) as excinfo:
        assert_password_acceptable(
            "password-erin-password", username="erin", settings=settings
        )

    assert excinfo.value.details["rule"] == "contains_username"


def test_weak_password_error_code() -> None:
    assert WeakPasswordError.code == "ansina.auth.weak_password"


def test_bundled_common_password_list_loads_and_is_nonempty() -> None:
    common = _common_passwords()

    assert len(common) > 1000
    assert "123456" not in common  # 6 chars — filtered out, below the 8-char floor
    assert "123456789" in common  # 9 chars — known top-10k entry, survives the filter
    assert all(entry == entry.casefold() for entry in common)
    assert all(len(entry) >= 8 for entry in common)
