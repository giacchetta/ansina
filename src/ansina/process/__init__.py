"""Shared subprocess primitives for any Ansina subsystem that spawns and
supervises a child process — see `handles.py`'s own module docstring.
"""

from ansina.process.handles import (
    PipedProcessHandle,
    PipedSpawner,
    ProcessHandle,
    Sleeper,
    Spawner,
    spawn_piped_subprocess,
    spawn_subprocess,
)

__all__ = [
    "PipedProcessHandle",
    "PipedSpawner",
    "ProcessHandle",
    "Sleeper",
    "Spawner",
    "spawn_piped_subprocess",
    "spawn_subprocess",
]
