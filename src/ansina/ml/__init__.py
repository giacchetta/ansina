"""Ansina's side of the ML-corpus consumer seam. See issue #63.

Ansina is a producer only — this package publishes the corpus contract's
machine-readable mirror (see `contract.py`); it contains no analysis logic and
never reads anything back from, or names, a consumer. `docs/ml/corpus-contract.md`
is the authoritative, human-readable half of the same contract.
"""

from __future__ import annotations

from ansina.ml.contract import (
    SCHEMA_PATH,
    SCHEMA_VERSION,
    contract_key,
    publish_contract_schema,
)

__all__ = [
    "SCHEMA_PATH",
    "SCHEMA_VERSION",
    "contract_key",
    "publish_contract_schema",
]
