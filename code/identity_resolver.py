"""Identity resolution for deletion-audit cases.

Scope of this module (Phase 1 blueprint; logic lands in Phase 2)
---------------------------------------------------------------
Turn the ``email`` supplied with a deletion request into the complete set of
identifiers the subject may still appear under anywhere in the platform:

* the stable internal reference (``user_id`` / ``user_NN``) that a tombstone must
  preserve (AGENTS.md section 6.5);
* the current email, phone, and email hash from ``user_identities.csv``;
* every historical email, phone, and email hash recovered from
  ``user_audit_log.csv``.

Design constraints established in Phase 1 (Task C):

1. The request email may be a *historical* value, and the chain can need more
   than one hop (``case_10``: ``nadia.ionescu@yahoo.com`` ->
   ``nadia.petrescu@yahoo.com`` -> ``n.petrescu@yahoo.com``). Walking a single
   hop stops on an address that is itself already stale.
2. Emails appear in mixed case in evidence and requests, so all comparisons are
   case-insensitive.
3. ``email`` and ``email_hash`` are always rewritten as a pair in the audit log,
   which is what lets an old hash still found in evidence be mapped back to the
   subject.
4. Only changes at or before the case's ``audit_at`` may be used, because the
   state has to be reconstructed as of that cutoff (AGENTS.md section 6.2).

No resolution logic is implemented yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
class IdentityRecord:
    """One row of ``user_identities.csv``: the current identity snapshot."""

    user_id: str
    name: str
    email: str
    phone: str
    email_hash: str
    updated_at: str


@dataclass(frozen=True)
class AuditEvent:
    """One row of ``user_audit_log.csv``: a historical identifier change."""

    audit_id: str
    user_id: str
    event_time: str
    field_name: str
    old_value: str
    new_value: str


@dataclass
class IdentifierSet:
    """Every identifier form the subject may still appear under."""

    user_id: str
    current_email: Optional[str] = None
    current_phone: Optional[str] = None
    current_email_hash: Optional[str] = None
    historical_emails: List[str] = field(default_factory=list)
    historical_phones: List[str] = field(default_factory=list)
    historical_email_hashes: List[str] = field(default_factory=list)

    @property
    def all_emails(self) -> List[str]:
        """Current plus historical emails, de-duplicated and order-preserving."""
        raise NotImplementedError("Phase 2: merge current and historical emails")

    @property
    def all_phones(self) -> List[str]:
        raise NotImplementedError("Phase 2: merge current and historical phones")

    @property
    def all_email_hashes(self) -> List[str]:
        raise NotImplementedError("Phase 2: merge current and historical hashes")


@dataclass(frozen=True)
class ResolutionResult:
    """Outcome of resolving one request email to a subject."""

    matched_user_id: Optional[str]
    matched_via: str  # current_email | historical_email | email_hash | name_phone | unresolved
    matched_value: str
    identifiers: Optional[IdentifierSet]
    hops: int = 0
    notes: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class IdentityResolver:
    """Resolve request emails to subjects across current and historical state."""

    def __init__(
        self,
        identities_csv: Path = config.USER_IDENTITIES_CSV,
        audit_log_csv: Path = config.USER_AUDIT_LOG_CSV,
        max_hops: int = config.MAX_IDENTITY_HOPS,
    ) -> None:
        self.identities_csv = Path(identities_csv)
        self.audit_log_csv = Path(audit_log_csv)
        self.max_hops = max_hops
        self._identities: Dict[str, IdentityRecord] = {}
        self._events_by_user: Dict[str, List[AuditEvent]] = {}

    # -- loading -----------------------------------------------------------

    def load(self) -> "IdentityResolver":
        """Read both CSVs into memory and index them by ``user_id``."""
        raise NotImplementedError("Phase 2: load user_identities + user_audit_log")

    # -- resolution --------------------------------------------------------

    def resolve(self, email: str, at: Optional[str] = None) -> ResolutionResult:
        """Resolve ``email`` to a subject, considering state as of ``at``.

        ``at`` is the case's ``audit_at``; audit events after it must be ignored.
        """
        raise NotImplementedError("Phase 2: multi-hop identity resolution")

    def identifiers_for(self, user_id: str, at: Optional[str] = None) -> IdentifierSet:
        """Build the full identifier set for a known subject."""
        raise NotImplementedError("Phase 2: assemble IdentifierSet")

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def normalise_email(value: str) -> str:
        """Lower-case and strip an email so comparisons are case-insensitive."""
        raise NotImplementedError("Phase 2: normalise email for matching")

    def match_any_identifier(self, value: str) -> List[ResolutionResult]:
        """Match a raw evidence value (email, hash, or phone) against all subjects."""
        raise NotImplementedError("Phase 2: reverse lookup for evidence linkage")
