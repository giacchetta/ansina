from __future__ import annotations

import pytest

from ansina.brain.retry import backoff_seconds


def test_first_retry_uses_initial_delay() -> None:
    assert backoff_seconds(1, initial=1.0, maximum=30.0) == 1.0


def test_delay_grows_geometrically() -> None:
    assert backoff_seconds(2, initial=1.0, maximum=30.0) == 2.0
    assert backoff_seconds(3, initial=1.0, maximum=30.0) == 4.0
    assert backoff_seconds(4, initial=1.0, maximum=30.0) == 8.0


def test_delay_is_capped_at_maximum() -> None:
    assert backoff_seconds(10, initial=1.0, maximum=30.0) == 30.0


def test_custom_multiplier() -> None:
    assert backoff_seconds(3, initial=1.0, maximum=100.0, multiplier=3.0) == 9.0


def test_attempt_below_one_rejected() -> None:
    with pytest.raises(ValueError, match="attempt must be >= 1"):
        backoff_seconds(0, initial=1.0, maximum=30.0)
