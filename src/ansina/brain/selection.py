"""`BrainProvider` factory. See issue #12.

Mirrors `ansina.heart.selection.build_heart_runtime`'s shape: fail loudly at
construction time (`BrainUnavailableError`, before `create_app`'s lifespan even
starts) rather than let a misconfiguration surface later as an opaque request failure.
There is no capability probe here the way MLX needs one — an OpenAI-compatible endpoint
has no host-hardware constraint — so the only thing to validate is that the
configuration is coherent enough to attempt a connection with.
"""

from __future__ import annotations

from ansina.brain.adapters.openai_compat import OpenAICompatibleBrainProvider
from ansina.brain.provider import BrainProvider
from ansina.config.settings import BrainSettings, Settings
from ansina.errors import BrainError
from ansina.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_BASE_URL = "https://api.openai.com/v1"


class BrainUnavailableError(BrainError):
    """`[brain] enabled = true` but the configuration can't be used. See module
    docstring — a keyless request against the default OpenAI host is the one case
    caught here; a keyless *custom* `base_url` (a local OpenAI-compatible server) is
    left alone.
    """

    code = "ansina.brain.unavailable"


def build_brain_provider(settings: Settings) -> BrainProvider:
    brain_settings: BrainSettings = settings.brain
    api_key = (
        brain_settings.api_key.get_secret_value()
        if brain_settings.api_key is not None
        else None
    )

    if api_key is None and brain_settings.base_url == _DEFAULT_BASE_URL:
        raise BrainUnavailableError(
            "brain.base_url is the default OpenAI host but no api_key is configured "
            "— set ANSINA_BRAIN__API_KEY, or point brain.base_url at a keyless "
            "OpenAI-compatible endpoint"
        )

    logger.info(
        "selected brain provider",
        extra={"base_url": brain_settings.base_url, "model": brain_settings.model},
    )
    return OpenAICompatibleBrainProvider(
        base_url=brain_settings.base_url,
        api_key=api_key,
        timeout_seconds=brain_settings.timeout_seconds,
        max_retries=brain_settings.max_retries,
        retry_initial_backoff_seconds=brain_settings.retry_initial_backoff_seconds,
        retry_max_backoff_seconds=brain_settings.retry_max_backoff_seconds,
        price_per_1m_input_tokens=brain_settings.price_per_1m_input_tokens,
        price_per_1m_output_tokens=brain_settings.price_per_1m_output_tokens,
    )
