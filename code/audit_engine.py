"""Deletion-audit engine: case orchestration and output serialization.

Scope of this module (Phase 1 blueprint; logic lands in Phase 2)
---------------------------------------------------------------
Own the per-case decision pipeline and the exact serialization of
``output.csv``:

    request -> authorized?  -> resolve identifiers -> reconstruct state at
    ``audit_at`` -> apply deletion_rules.csv -> FINDING / REMEDIATION /
    VERIFICATION steps -> one output row per ``case_id``

Design constraints established in Phase 1 (Task C):

* ``COMPLETE`` cases contain only ``VERIFICATION`` steps, and every one of them
  cites a non-empty ``evidence_id``.
* ``INCOMPLETE`` cases contain findings followed by the ordered remediation plan
  and post-remediation checks; only ``FINDING`` steps carry an ``evidence_id``.
* ``NOT_APPLICABLE`` cases always serialize ``steps_json`` as ``[]``.
* Phase order is ``FINDING`` -> ``REMEDIATION`` -> ``VERIFICATION``, and
  ``step_no`` runs contiguously from 1 across the whole array, not per phase.
* Within remediation, data flows and restores are neutralised before the records
  that depend on them.
* No evidence captured after the case's ``audit_at`` may be cited.

No evaluation loops or rule parsers are implemented yet.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

try:
    import config
except ImportError:  # pragma: no cover - direct script execution
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditStep:
    """One element of ``steps_json``; exactly the seven contract fields."""

    step_no: int
    phase: str
    system_id: str
    resource_id: str
    operation: str
    state: str
    evidence_id: str

    def to_dict(self) -> Dict[str, Any]:
        """Return the step as an ordered dict using the contract field order."""
        return {name: getattr(self, name) for name in config.STEP_FIELDS}

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "AuditStep":
        return cls(**{name: payload[name] for name in config.STEP_FIELDS})

    def validate(self) -> None:
        """Raise ``ValueError`` if the step violates the contract in section 6.4."""
        raise NotImplementedError("Phase 2: validate phase/operation/state combination")


@dataclass
class CaseAudit:
    """The decided answer for a single ``case_id``."""

    case_id: str
    deletion_status: str
    steps: List[AuditStep] = field(default_factory=list)

    def to_row(self) -> Dict[str, str]:
        """Serialize to the ``case_id,deletion_status,steps_json`` output row."""
        return {
            "case_id": self.case_id,
            "deletion_status": self.deletion_status,
            "steps_json": serialize_steps(self.steps),
        }

    def validate(self) -> None:
        raise NotImplementedError("Phase 2: validate status/phase ordering rules")


def serialize_steps(steps: Sequence[AuditStep]) -> str:
    """Encode steps as deterministic JSON: no whitespace drift, array order kept."""
    return json.dumps(
        [step.to_dict() for step in steps],
        separators=config.JSON_DUMP_SEPARATORS,
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class DeletionAuditEngine:
    """Decide one case at a time and emit the full ``output.csv``."""

    def __init__(
        self,
        rules_csv: Path = config.DELETION_RULES_CSV,
        evidence_manifest_csv: Path = config.EVIDENCE_FILES_CSV,
        activity_csv: Path = config.SYSTEM_ACTIVITY_CSV,
    ) -> None:
        self.rules_csv = Path(rules_csv)
        self.evidence_manifest_csv = Path(evidence_manifest_csv)
        self.activity_csv = Path(activity_csv)
        self._rules: List[Dict[str, str]] = []
        self._evidence_index: Dict[str, Dict[str, str]] = {}

    # -- setup -------------------------------------------------------------

    def load_policy(self) -> None:
        """Load ``deletion_rules.csv`` and the evidence manifest."""
        raise NotImplementedError("Phase 2: load rules and evidence manifest")

    # -- per-case pipeline -------------------------------------------------

    def authorizes_deletion(self, request_text: str) -> bool:
        """Decide whether the request text actually authorizes deletion."""
        raise NotImplementedError("Phase 2: request-text classification")

    def run_case(self, request: Dict[str, str], tracker: Any = None) -> CaseAudit:
        """Produce the decided answer for one request row."""
        raise NotImplementedError("Phase 2: per-case audit pipeline")

    # -- sweep -------------------------------------------------------------

    def run_sweep(
        self,
        requests_csv: Path = config.DELETION_REQUESTS_CSV,
        output_csv: Path = config.OUTPUT_CSV,
        tracker: Any = None,
    ) -> List[CaseAudit]:
        """Audit every request and write one output row per ``case_id``."""
        raise NotImplementedError("Phase 2: full dataset sweep")

    @staticmethod
    def write_output(results: Iterable[CaseAudit], path: Path = config.OUTPUT_CSV) -> Path:
        """Write ``output.csv`` with the contract columns, sorted by ``case_id``."""
        raise NotImplementedError("Phase 2: deterministic output.csv writing")
