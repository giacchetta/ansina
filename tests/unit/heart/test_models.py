from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ansina.config.settings import HeartSettings
from ansina.heart.models import ResolvedModel, resolve_model
from ansina.heart.runtime import HeartLoadError, HeartUnavailableError


def test_model_path_wins_and_requires_no_downloader(tmp_path: Path) -> None:
    model_dir = tmp_path / "my-model"
    model_dir.mkdir()
    settings = HeartSettings(model_path=model_dir)

    resolved = resolve_model(settings, downloader=lambda *_: pytest.fail("network hit"))

    assert resolved == ResolvedModel(path=model_dir, source="path")


def test_missing_model_path_raises_load_error(tmp_path: Path) -> None:
    settings = HeartSettings(model_path=tmp_path / "does-not-exist")

    with pytest.raises(HeartLoadError, match="does not exist"):
        resolve_model(settings)


def test_no_model_path_fetches_via_repo_using_injected_downloader(
    tmp_path: Path,
) -> None:
    settings = HeartSettings(model_repo="org/some-model", cache_dir=tmp_path)
    calls: list[tuple[str, Path]] = []

    def _fake_downloader(repo_id: str, cache_dir: Path) -> Path:
        calls.append((repo_id, cache_dir))
        return tmp_path / "cached-snapshot"

    resolved = resolve_model(settings, downloader=_fake_downloader)

    assert resolved == ResolvedModel(path=tmp_path / "cached-snapshot", source="repo")
    assert calls == [("org/some-model", tmp_path)]


def test_missing_huggingface_hub_raises_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = HeartSettings(model_repo="org/some-model", cache_dir=tmp_path)
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)

    with pytest.raises(HeartUnavailableError, match="huggingface_hub"):
        resolve_model(settings)


def test_downloads_via_huggingface_hub_snapshot_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exercises `_download_from_hub`'s real body (not just the injected-downloader
    seam) by installing a stub `huggingface_hub` module.
    """
    settings = HeartSettings(model_repo="org/some-model", cache_dir=tmp_path)
    calls: list[dict[str, str]] = []

    class _StubHub:
        @staticmethod
        def snapshot_download(*, repo_id: str, cache_dir: str) -> str:
            calls.append({"repo_id": repo_id, "cache_dir": cache_dir})
            return str(tmp_path / "snapshot")

    monkeypatch.setitem(sys.modules, "huggingface_hub", _StubHub())

    resolved = resolve_model(settings)

    assert resolved == ResolvedModel(path=tmp_path / "snapshot", source="repo")
    assert calls == [{"repo_id": "org/some-model", "cache_dir": str(tmp_path)}]
