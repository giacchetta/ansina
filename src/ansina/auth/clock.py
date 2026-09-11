"""A shared, injectable clock plus the ISO 8601 formatting/parsing pair that depends
on it — moved out of `auth.sudo` (issue #26) once a second caller (issue #28's
coalesced `last_used_at` bookkeeping in `auth.authenticator`) needed the exact same
thing, the way `api.identity` was lifted out of two private copies in issue #30.

Every timestamp this codebase writes for a caller-supplied "now" — `sudo_grants`,
`sudo_lockouts`, and (issue #28) `credentials.last_used_at` — goes through `iso()`, and
every comparison against one goes through `parse_iso()` or, more often, a direct string
comparison against another `iso()` value (millisecond-precision ISO 8601 UTC sorts
identically as text, which every caller here relies on instead of parsing both sides).
`utc_now()` is the real-clock default; every caller accepts an injectable `Clock`
instead so TTL/staleness/expiry logic is testable without real sleeping.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    """Millisecond-precision ISO 8601 UTC, matching `0002_rbac.sql`'s
    `strftime('%Y-%m-%dT%H:%M:%fZ', 'now')` column defaults closely enough that the
    two sort identically as text — every expiry/staleness comparison in this codebase
    (`SudoGrantRepository.find_active`'s `expires_at > ?`, issue #28's `last_used_at`
    staleness check) depends on that.
    """
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
