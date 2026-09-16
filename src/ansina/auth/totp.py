"""RFC 6238 TOTP: pure code generation and verification, no storage. See issue #41.

Hand-rolled on stdlib `hmac`/`hashlib`/`struct` per the milestone's own scope decision
— no extra library for something this small and this security-critical to get subtly
wrong by depending on someone else's untested wrapper. SHA-1 only: RFC 6238's baseline
mode, and what every mainstream authenticator app (Google Authenticator, Authy, ...)
implements — verified against the RFC's own published test vectors (Appendix B) in
`tests/unit/auth/test_totp.py`.

Anti-replay is `find_valid_step`'s job, not a side effect of generation:
`ansina.auth.step_up.TotpStepUpVerifier` is the one caller, and it owns persisting the
returned step index as the new floor.
"""

from __future__ import annotations

import hashlib
import hmac
import struct

DEFAULT_DIGITS = 6
DEFAULT_STEP_SECONDS = 30
# RFC 4226 §4 recommends >= 128 bits of secret; RFC 6238's reference implementation and
# every mainstream authenticator app use 160 bits (20 bytes) for HMAC-SHA1 — this
# generates a secret compatible with those apps, not just with this codebase's own
# verifier.
SECRET_BYTES = 20
# RFC 6238 §5.2's suggested default: one step of clock drift tolerated on either side
# of "now".
DEFAULT_WINDOW = 1


def hotp(secret: bytes, counter: int, *, digits: int = DEFAULT_DIGITS) -> str:
    """RFC 4226 HOTP — the counter-based primitive TOTP layers a time-derived counter
    on top of.
    """
    counter_bytes = struct.pack(">Q", counter)
    mac = hmac.new(secret, counter_bytes, hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    truncated = mac[offset : offset + 4]
    code_int = (int.from_bytes(truncated, "big") & 0x7FFFFFFF) % (10**digits)
    return str(code_int).zfill(digits)


def step_index(at: int, *, step_seconds: int = DEFAULT_STEP_SECONDS) -> int:
    """`at` (Unix epoch seconds) as an RFC 6238 step counter: `T = floor((Current
    Unix time - T0) / X)` with `T0 = 0`.
    """
    return at // step_seconds


def totp_code(
    secret: bytes,
    *,
    at: int,
    step_seconds: int = DEFAULT_STEP_SECONDS,
    digits: int = DEFAULT_DIGITS,
) -> str:
    """The code valid at Unix time `at`."""
    return hotp(secret, step_index(at, step_seconds=step_seconds), digits=digits)


def find_valid_step(
    secret: bytes,
    code: str,
    *,
    at: int,
    step_seconds: int = DEFAULT_STEP_SECONDS,
    digits: int = DEFAULT_DIGITS,
    window: int = DEFAULT_WINDOW,
    not_before_step: int | None = None,
) -> int | None:
    """The step index `code` matches, within `window` steps of `at` in either
    direction — or `None` if it matches none of them.

    `not_before_step` is the anti-replay floor (exclusive): a candidate step at or
    before it is never matched, even if the code is otherwise correct, so a previously
    -accepted code — or an older, leaked one — can never be replayed. Every candidate
    is compared with `hmac.compare_digest`, never `==`, on the code itself.
    """
    current_step = step_index(at, step_seconds=step_seconds)
    for delta in range(-window, window + 1):
        candidate_step = current_step + delta
        if not_before_step is not None and candidate_step <= not_before_step:
            continue
        candidate_code = hotp(secret, candidate_step, digits=digits)
        if hmac.compare_digest(candidate_code, code):
            return candidate_step
    return None
