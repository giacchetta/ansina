"""Heart model resolution: an explicit local path, or a Hugging Face repo id fetched
into a local, on-disk, download-once cache. See issue #10.

`resolve_model` never imports `huggingface_hub` at module scope — the download path is
behind an optional extra (`ansina[mlx]`), so importing `ansina.heart` must stay clean
even when it's absent. A missing package surfaces as `HeartUnavailableError`, not an
`ImportError` bubbling out of this module.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ansina.config.settings import HeartSettings
from ansina.heart.runtime import HeartLoadError, HeartUnavailableError
from ansina.logging import get_logger

logger = get_logger(__name__)

# `(repo_id, cache_dir) -> local snapshot directory`. Injected by tests so
# `resolve_model` never touches the network in the unit suite; the default
# implementation is `_download_from_hub` below.
Downloader = Callable[[str, Path], Path]


@dataclass(frozen=True)
class ResolvedModel:
    path: Path
    source: Literal["path", "repo"]


def _download_from_hub(repo_id: str, cache_dir: Path) -> Path:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise HeartUnavailableError(
            "no local heart.model_path is configured and huggingface_hub is not "
            "installed to fetch heart.model_repo "
            f"({repo_id!r}) — install it via `uv sync --extra mlx`"
        ) from exc

    logger.info(
        "resolving heart model",
        extra={"model_repo": repo_id, "cache_dir": str(cache_dir)},
    )
    return Path(snapshot_download(repo_id=repo_id, cache_dir=str(cache_dir)))


def resolve_model(
    settings: HeartSettings, *, downloader: Downloader | None = None
) -> ResolvedModel:
    """An explicit `model_path` always wins, with no network access, and must exist.
    Otherwise `model_repo` is fetched into `cache_dir` (a warm cache resolves purely
    locally — that's the "download once, reuse" cache issue #10 asks for).
    """
    if settings.model_path is not None:
        if not settings.model_path.exists():
            raise HeartLoadError(
                f"heart.model_path {str(settings.model_path)!r} does not exist"
            )
        return ResolvedModel(path=settings.model_path, source="path")

    download = downloader if downloader is not None else _download_from_hub
    path = download(settings.model_repo, settings.cache_dir)
    return ResolvedModel(path=path, source="repo")
