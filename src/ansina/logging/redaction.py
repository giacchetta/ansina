"""Redaction applied at the formatter level — see `formatter.JsonFormatter`.

Two layers, both applied to every string that reaches a log sink:

1. A small set of regex patterns for secret *shapes* (bearer tokens, `key=value`-style
   assignments, common vendor token prefixes) that catch secrets Ansina never
   configured but that leaked into a log line anyway (e.g. copy-pasted into an error
   message).
2. A registry of literal values (`register_secret`) seeded with whatever Ansina
   actually has configured (e.g. `settings.security.api_token`), so the real secret
   is masked verbatim even in a shape no pattern anticipates.

Deliberately minimal — start small, extend the pattern list as a real gap is found
rather than pre-building an OpenClaw-sized (~1,400 line) redaction engine no one has
needed yet.
"""

from __future__ import annotations

import re
from functools import partial

REDACTED = "***"

# Minimum length for a registered literal — guards against a short dev/placeholder value
# (e.g. "test") redacting unrelated text throughout a log line.
_MIN_SECRET_LENGTH = 8

# Each pattern is paired with the indices of the capture group(s) that hold the secret
# *value* — the only part `_mask_match` replaces. Surrounding groups (a key name, a
# quote character) are structural and must survive so the redacted line stays readable.
_PATTERNS: tuple[tuple[re.Pattern[str], tuple[int, ...]], ...] = (
    # `Authorization: Bearer <token>` and bare `Bearer <token>`.
    (re.compile(r"(?i)\bBearer\s+([A-Za-z0-9\-._~+/]+=*)"), (1,)),
    # `key=value` / `key: "value"` assignments for common secret-shaped keys. Group 1 is
    # the key name and group 2 the optional quote character — both left intact. The
    # negative lookahead keeps this from treating a bare `Bearer` scheme word as the
    # value when it runs after the Bearer pattern has already masked the real token.
    (
        re.compile(
            r"(?i)\b(token|api[-_]?key|secret|password|passwd|authorization|credential)"
            r"\s*[:=]\s*(\"?)(?!Bearer\b)([^\s\"',}]+)\2"
        ),
        (3,),
    ),
    # Common vendor token prefixes, wherever they appear.
    (
        re.compile(
            r"\b(sk-[A-Za-z0-9]{16,}|gh[po]_[A-Za-z0-9]{16,}|xox[baprs]-[A-Za-z0-9-]{16,})"
        ),
        (1,),
    ),
)

_registered_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register a literal value (e.g. a configured API token) for verbatim redaction.

    Ignores `None` and values shorter than `_MIN_SECRET_LENGTH` — see module
    docstring. Call `SecretStr.get_secret_value()` before passing one in; this module
    only ever handles plain `str`.
    """
    if value is not None and len(value) >= _MIN_SECRET_LENGTH:
        _registered_secrets.add(value)


def clear_secrets() -> None:
    """Drop all registered literals — test isolation only."""
    _registered_secrets.clear()


def redact(text: str) -> str:
    """Mask registered secret literals and known secret-shaped patterns in `text`."""
    result = text
    # Longest first, so one registered secret can't be a prefix that partially masks
    # another and leaves a confusing remainder behind.
    for secret in sorted(_registered_secrets, key=len, reverse=True):
        result = result.replace(secret, REDACTED)
    for pattern, value_groups in _PATTERNS:
        result = pattern.sub(partial(_mask_match, value_groups=value_groups), result)
    return result


def _mask_match(match: re.Match[str], value_groups: tuple[int, ...]) -> str:
    """Replace only `value_groups` of `match`, keeping the rest of its span intact.

    Splices out each named group's own span, rightmost first, so earlier offsets in the
    matched text stay valid as later ones are replaced.
    """
    base = match.start()
    replaced = match.group(0)
    for group_index in sorted(value_groups, reverse=True):
        local_start, local_end = (
            match.start(group_index) - base,
            match.end(group_index) - base,
        )
        replaced = replaced[:local_start] + REDACTED + replaced[local_end:]
    return replaced
