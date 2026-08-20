"""Ansina's REST API surface. See issue #4.

`create_app()` is the one factory the rest of the codebase (and `ansina.__main__`) uses
to build the FastAPI application — mirrors `config.load_settings()` /
`logging.configure_logging()` as the single typed entry point per subsystem.
"""

from ansina.api.app import create_app

__all__ = ["create_app"]
