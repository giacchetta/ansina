from __future__ import annotations

from ansina.brain.events import RETRYABLE_ERROR_CLASSES, BrainErrorClass, BrainUsage


def test_retryable_classes() -> None:
    assert {
        BrainErrorClass.TIMEOUT,
        BrainErrorClass.RATE_LIMIT,
        BrainErrorClass.TRANSPORT,
        BrainErrorClass.PROVIDER_SERVER,
    } == RETRYABLE_ERROR_CLASSES


def test_client_and_internal_errors_are_not_retryable() -> None:
    assert BrainErrorClass.PROVIDER_CLIENT not in RETRYABLE_ERROR_CLASSES
    assert BrainErrorClass.INTERNAL not in RETRYABLE_ERROR_CLASSES


def test_usage_defaults_to_no_cost() -> None:
    usage = BrainUsage(
        prompt_tokens=10, completion_tokens=5, total_tokens=15, authoritative=True
    )

    assert usage.cost_usd is None
