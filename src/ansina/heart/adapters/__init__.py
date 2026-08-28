"""`HeartRuntime` adapters. See issue #10.

`mlx.py` is the only adapter this milestone ships (Apple Silicon, matching the primary
deployment target — a Mac Mini M4). A portable, non-Apple-Silicon fallback is tracked
in a follow-up issue, deferred because no llama-cpp-python-compatible GPU was available
to prove it against; see `ansina.heart.selection` for the capability probe that fails
loudly rather than silently degrading in the meantime.
"""
