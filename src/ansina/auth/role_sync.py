"""`sync_mapped_roles` — the claims-to-roles reconciliation primitive. See issue #42.

Ships and is tested standalone, against a synthetic claims dict, with no caller yet:
#43's OIDC login exchange is the only intended caller, added in a follow-up issue
specifically so the provenance rules here are proven independent of any code path that
is also validating a JWT.

The whole point of this function is what it *never* touches: every read and write is
scoped to `role_assignments` rows whose `source` equals the `provider` argument exactly
(`RoleAssignmentRepository.role_ids_for_subject`/`.assign`/`.unassign`, all issue #42).
A `source='local'` row (an Admin's manual grant) or a different provider's row is never
in the diff this function computes — that is what lets an IdP-side revoke take effect
on next login without ever fighting or wiping a manually-made assignment.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ansina.auth.models import SubjectType
from ansina.auth.repositories import RoleAssignmentRepository, RoleMappingRepository
from ansina.storage.database import Database


def _claim_matches(claims: Mapping[str, Any], claim: str, value: str) -> bool:
    """`True` if `claims[claim]` matches `value` — a plain string claim matches on
    equality; a list/tuple claim (e.g. a `groups` array) matches if any string element
    equals `value`. Anything else (an int, a bool, `None`, a nested mapping, or a
    missing claim key) never matches — fail-closed, since a role grant is the stake.
    """
    claim_value = claims.get(claim)
    if isinstance(claim_value, str):
        return claim_value == value
    if isinstance(claim_value, list | tuple):
        return any(isinstance(item, str) and item == value for item in claim_value)
    return False


def sync_mapped_roles(
    db: Database, user_id: str, provider: str, claims: Mapping[str, Any]
) -> None:
    """Reconcile `user_id`'s `provider`-sourced role assignments to match `claims`
    exactly: resolve every `role_mappings` row for `provider` whose `(claim, value)`
    matches something in `claims`, then add whatever's missing and remove whatever's no
    longer implied — scoped to `source=provider` throughout, per this module's
    docstring.

    Idempotent: calling this again with the same `claims` diffs to no changes. A role
    manually assigned (`source='local'`) with the same `role_id` a mapping would also
    grant is untouched either way — `RoleAssignmentRepository.assign`'s
    `ON CONFLICT ... DO NOTHING` never overwrites an existing row's `source`, so that
    grant simply isn't counted as "current" for this provider and is never a candidate
    for removal.
    """
    mappings = RoleMappingRepository(db).list_for_provider(provider)
    desired = frozenset(
        mapping.role_id
        for mapping in mappings
        if _claim_matches(claims, mapping.claim, mapping.value)
    )

    assignments = RoleAssignmentRepository(db)
    current = assignments.role_ids_for_subject(
        SubjectType.USER, user_id, source=provider
    )

    for role_id in desired - current:
        assignments.assign(SubjectType.USER, user_id, role_id, source=provider)
    for role_id in current - desired:
        assignments.unassign(SubjectType.USER, user_id, role_id, source=provider)
