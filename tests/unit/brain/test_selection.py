from __future__ import annotations

from pathlib import Path

import pytest

from ansina.brain.adapters.openai_compat import OpenAICompatibleBrainProvider
from ansina.brain.selection import BrainUnavailableError, build_brain_provider
from ansina.config import Settings, load_settings


@pytest.fixture
def enabled_settings(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> Settings:
    monkeypatch.setenv("ANSINA_BRAIN__ENABLED", "true")
    return load_settings()


def test_keyless_default_host_is_refused(enabled_settings: Settings) -> None:
    with pytest.raises(BrainUnavailableError, match="api_key"):
        build_brain_provider(enabled_settings)


def test_keyed_default_host_builds_provider(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_BRAIN__ENABLED", "true")
    monkeypatch.setenv("ANSINA_BRAIN__API_KEY", "s3cr3t-brain-key-0123")
    settings = load_settings()

    provider = build_brain_provider(settings)

    assert isinstance(provider, OpenAICompatibleBrainProvider)


def test_keyless_custom_host_is_allowed(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local OpenAI-compatible server rarely needs a real key — only a keyless
    request against the *default* OpenAI host is refused.
    """
    monkeypatch.setenv("ANSINA_BRAIN__ENABLED", "true")
    monkeypatch.setenv("ANSINA_BRAIN__BASE_URL", "http://localhost:11434/v1")
    settings = load_settings()

    provider = build_brain_provider(settings)

    assert isinstance(provider, OpenAICompatibleBrainProvider)
