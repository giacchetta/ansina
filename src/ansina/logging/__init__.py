"""JSON structured logging with formatter-level redaction. See issue #3.

Boot sequence: call `configure_logging(load_settings())` once, then use
`get_logger(__name__)` everywhere else — mirrors `ansina.config.load_settings()` as
the one typed entry point.
"""

from ansina.logging.context import get_request_id, request_id_scope
from ansina.logging.formatter import JsonFormatter
from ansina.logging.redaction import redact, register_secret
from ansina.logging.setup import configure_logging, get_logger

__all__ = [
    "JsonFormatter",
    "configure_logging",
    "get_logger",
    "get_request_id",
    "redact",
    "register_secret",
    "request_id_scope",
]
