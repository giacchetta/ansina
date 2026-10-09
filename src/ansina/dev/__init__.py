"""Dev Mode (issue #62): `ansina --dev`'s lab/pre-customer posture — preflighting,
spawning, and supervising the Vector telemetry sidecar so the daemon's rotated
`[telemetry]` spool files actually reach an S3-compatible bucket. See
`posture.py`'s own module docstring for where this is established (`__main__.py`,
before uvicorn binds) and why it is never wired into `create_app()`'s lifespan.
"""

from ansina.dev.posture import dev_mode
from ansina.dev.vector import build_vector_sidecar

__all__ = ["build_vector_sidecar", "dev_mode"]
