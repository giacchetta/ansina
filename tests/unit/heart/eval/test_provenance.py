from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from ansina.heart.eval.provenance import Provenance, resolve_provenance


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )


def _init_repo(path: Path, *, branch: str = "m6-heartbeat") -> None:
    _git(path, "init", "--quiet", "--initial-branch", branch)
    _git(path, "commit", "--allow-empty", "--quiet", "-m", "init")


def test_resolve_provenance_returns_commit_and_branch_for_a_clean_checkout(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)

    provenance = resolve_provenance(cwd=tmp_path)

    assert provenance.branch == "m6-heartbeat"
    assert provenance.commit is not None
    assert not provenance.commit.endswith("-dirty")
    assert len(provenance.commit) == 40  # a bare, unsuffixed full sha


def test_resolve_provenance_suffixes_dirty_on_an_unclean_worktree(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "untracked.txt").write_text("hello\n")

    provenance = resolve_provenance(cwd=tmp_path)

    assert provenance.commit is not None
    assert provenance.commit.endswith("-dirty")


def test_resolve_provenance_returns_none_none_for_a_non_checkout(
    tmp_path: Path,
) -> None:
    provenance = resolve_provenance(cwd=tmp_path)

    assert provenance == Provenance(commit=None, branch=None)


def test_resolve_provenance_returns_none_none_when_git_is_absent(
    tmp_path: Path,
) -> None:
    def _raise(
        args: Sequence[str],
        *,
        cwd: Path | None,
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        raise OSError("git: command not found")

    provenance = resolve_provenance(cwd=tmp_path, run=_raise)

    assert provenance == Provenance(commit=None, branch=None)
