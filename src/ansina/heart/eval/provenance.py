"""Resolves the git commit/branch a bench report should be stamped with. See issue
#58 — a committed bench report should be tied to the code that produced it, so
`__main__.py` calls `resolve_provenance()` on every run (local or via
`scripts/remote-heart.sh`), not only remote ones.

`subprocess` stays confined to this one dev-tooling module — the daemon itself never
shells out, and `ansina.heart.eval` already isn't imported by anything the daemon
loads at runtime (see `heart/eval/__main__.py`'s own module docstring).
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class GitRunner(Protocol):
    """The exact slice of `subprocess.run`'s signature this module calls —
    narrow enough that `subprocess.run` itself satisfies it with no wrapper, and
    the unit suite can inject a fake without a real `git` binary.
    """

    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None,
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True, slots=True)
class Provenance:
    """`commit` carries a `-dirty` suffix (the `git describe --dirty` convention)
    when the worktree had uncommitted changes at resolve time. Both fields are
    `None` together — never one `None` and the other set — whenever provenance
    can't be determined at all (no `git`, or `cwd` isn't a checkout).
    """

    commit: str | None
    branch: str | None


def resolve_provenance(
    *, cwd: Path | None = None, run: GitRunner = subprocess.run
) -> Provenance:
    """Never raises — a bench run must never fail because provenance metadata is
    unavailable. Any failure (missing `git`, `cwd` not a checkout, any non-zero
    exit) resolves to `Provenance(None, None)`.
    """

    def _git(*args: str) -> str | None:
        try:
            completed = run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout.strip()

    commit = _git("rev-parse", "HEAD")
    if commit is None:
        return Provenance(commit=None, branch=None)

    if _git("status", "--porcelain"):
        commit = f"{commit}-dirty"

    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    return Provenance(commit=commit, branch=branch)
