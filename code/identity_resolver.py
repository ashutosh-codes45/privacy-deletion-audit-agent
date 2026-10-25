"""Identity resolution for deletion-audit cases.

Responsibility
--------------
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
"""

from __future__ import annotations

import csv
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
# Normalisation helpers (shared with the audit engine)
# ---------------------------------------------------------------------------


def normalise_email(value: Optional[str]) -> str:
    """Lower-case and strip a value so email comparisons are case-insensitive."""
    if not value:
        return ""
    return value.strip().lower()


def normalise_phone(value: Optional[str]) -> str:
    """Reduce a phone number to its digits so formatting differences collapse."""
    if not value:
        return ""
    return "".join(ch for ch in value if ch.isdigit())


def normalise_hash(value: Optional[str]) -> str:
    """Lower-case and strip a hex digest."""
    if not value:
        return ""
    return value.strip().lower()


def _dedupe(values) -> List[str]:
    """De-duplicate strings, preserving first-seen order and dropping blanks."""
    seen = set()
    ordered: List[str] = []
    for value in values:
        if not value:
            continue
        key = str(value).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    return ordered


def _is_admissible(timestamp: str, at: Optional[str]) -> bool:
    """True when ``timestamp`` is at or before the ``at`` cutoff.

    Timestamps in the corpus share the ``YYYY-MM-DDTHH:MM:SSZ`` layout, so a
    plain lexicographic comparison is correct and avoids timezone surprises.
    """
    if not at:
        return True
    if not timestamp:
        return True
    return timestamp.strip() <= at.strip()


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
        return _dedupe([self.current_email] + list(self.historical_emails))

    @property
    def all_phones(self) -> List[str]:
        """Current plus historical phone numbers, de-duplicated."""
        return _dedupe([self.current_phone] + list(self.historical_phones))

    @property
    def all_email_hashes(self) -> List[str]:
        """Current plus historical email hashes, de-duplicated."""
        return _dedupe([self.current_email_hash] + list(self.historical_email_hashes))

    def as_search_keys(self) -> Dict[str, set]:
        """Return normalised lookup sets used to match evidence records."""
        return {
            "emails": {normalise_email(v) for v in self.all_emails if normalise_email(v)},
            "phones": {normalise_phone(v) for v in self.all_phones if normalise_phone(v)},
            "hashes": {normalise_hash(v) for v in self.all_email_hashes if normalise_hash(v)},
            "refs": {self.user_id.strip().lower()} if self.user_id else set(),
        }


@dataclass(frozen=True)
class ResolutionResult:
    """Outcome of resolving one request email to a subject."""

    matched_user_id: Optional[str]
    matched_via: str  # current_email | historical_email | email_hash | phone | unresolved
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
        self._events: List[AuditEvent] = []
        # Reverse lookups: any known identifier form -> stable user_id.
        self._email_to_user: Dict[str, str] = {}
        self._hash_to_user: Dict[str, str] = {}
        self._phone_to_user: Dict[str, str] = {}
        # Email change chain: old_lower -> (new_lower, user_id, event_time).
        self._email_edge: Dict[str, Tuple[str, str, str]] = {}
        self._loaded = False

    # -- loading -----------------------------------------------------------

    def load(self) -> "IdentityResolver":
        """Read both CSVs into memory and index them by ``user_id``."""
        with open(self.identities_csv, "r", encoding=config.CSV_ENCODING, newline="") as fh:
            for row in csv.DictReader(fh):
                user_id = (row.get("user_id") or "").strip()
                if not user_id:
                    continue
                self._identities[user_id] = IdentityRecord(
                    user_id=user_id,
                    name=(row.get("name") or "").strip(),
                    email=(row.get("email") or "").strip(),
                    phone=(row.get("phone") or "").strip(),
                    email_hash=(row.get("email_hash") or "").strip(),
                    updated_at=(row.get("updated_at") or "").strip(),
                )

        with open(self.audit_log_csv, "r", encoding=config.CSV_ENCODING, newline="") as fh:
            for row in csv.DictReader(fh):
                user_id = (row.get("user_id") or "").strip()
                if not user_id:
                    continue
                event = AuditEvent(
                    audit_id=(row.get("audit_id") or "").strip(),
                    user_id=user_id,
                    event_time=(row.get("event_time") or "").strip(),
                    field_name=(row.get("field_name") or "").strip(),
                    old_value=(row.get("old_value") or "").strip(),
                    new_value=(row.get("new_value") or "").strip(),
                )
                self._events.append(event)
                self._events_by_user.setdefault(user_id, []).append(event)

        self._build_indexes()
        self._loaded = True
        return self

    def _build_indexes(self) -> None:
        """Index every current and historical identifier value to its subject."""
        for user_id, record in self._identities.items():
            email = normalise_email(record.email)
            if email:
                self._email_to_user.setdefault(email, user_id)
            digest = normalise_hash(record.email_hash)
            if digest:
                self._hash_to_user.setdefault(digest, user_id)
            phone = normalise_phone(record.phone)
            if phone:
                self._phone_to_user.setdefault(phone, user_id)

        for event in self._events:
            user_id = event.user_id
            field_name = event.field_name.strip().lower()
            if field_name == "email":
                old_email = normalise_email(event.old_value)
                new_email = normalise_email(event.new_value)
                for value in (old_email, new_email):
                    if value:
                        self._email_to_user.setdefault(value, user_id)
                if old_email and new_email:
                    self._email_edge.setdefault(
                        old_email, (new_email, user_id, event.event_time)
                    )
            elif field_name == "email_hash":
                for value in (event.old_value, event.new_value):
                    digest = normalise_hash(value)
                    if digest:
                        self._hash_to_user.setdefault(digest, user_id)
            elif field_name == "phone":
                for value in (event.old_value, event.new_value):
                    phone = normalise_phone(value)
                    if not phone:
                        continue
                    self._phone_to_user.setdefault(phone, user_id)

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # -- resolution --------------------------------------------------------

    def resolve(self, email: str, at: Optional[str] = None) -> ResolutionResult:
        """Resolve ``email`` to a subject, considering state as of ``at``.

        ``at`` is the case's ``audit_at``; audit events after it are ignored
        when the identifier set is assembled.
        """
        self._ensure_loaded()
        key = normalise_email(email)
        if not key:
            return ResolutionResult(None, "unresolved", email, None, 0, ("empty request email",))

        user_id = self._email_to_user.get(key)
        matched_via = "unresolved"

        if user_id is not None:
            record = self._identities.get(user_id)
            if record and normalise_email(record.email) == key:
                matched_via = "current_email"
            else:
                matched_via = "historical_email"
        else:
            digest = normalise_hash(email)
            if digest and digest in self._hash_to_user:
                user_id = self._hash_to_user[digest]
                matched_via = "email_hash"
            else:
                phone = normalise_phone(email)
                if phone and phone in self._phone_to_user:
                    user_id = self._phone_to_user[phone]
                    matched_via = "phone"

        if user_id is None:
            return ResolutionResult(
                None, "unresolved", email, None, 0, ("no current or historical identifier matched",)
            )

        hops = self._chain_hops(key, user_id, at)
        identifiers = self.identifiers_for(user_id, at)
        notes: Tuple[str, ...] = ()
        if matched_via == "historical_email":
            notes = (
                f"request email is a historical value for {user_id}; "
                f"resolved after {hops} change hop(s)",
            )
        return ResolutionResult(user_id, matched_via, email, identifiers, hops, notes)

    def _chain_hops(self, start_key: str, user_id: str, at: Optional[str]) -> int:
        """Count the email change hops needed to walk ``start_key`` to the live value.

        The walk follows the ``old_value -> new_value`` edges recorded in the
        audit log, which is what makes a multi-hop request email resolvable.
        """
        value = start_key
        seen = {value}
        hops = 0
        while hops < self.max_hops:
            edge = self._email_edge.get(value)
            if edge is None:
                break
            new_value, edge_user, event_time = edge
            if edge_user != user_id:
                break
            if at and not _is_admissible(event_time, at):
                break
            if new_value in seen:
                break
            seen.add(new_value)
            value = new_value
            hops += 1
        return hops

    def identifiers_for(self, user_id: str, at: Optional[str] = None) -> IdentifierSet:
        """Build the full identifier set for a known subject.

        The current snapshot is always included; every audit-log value at or
        before the ``at`` cutoff is added so that a leak in a legacy system which
        still holds an old address can be attributed to the subject.
        """
        self._ensure_loaded()
        record = self._identities.get(user_id)
        emails: List[str] = []
        phones: List[str] = []
        hashes: List[str] = []

        for event in self._events_by_user.get(user_id, ()):
            if not _is_admissible(event.event_time, at):
                continue
            field_name = event.field_name.strip().lower()
            if field_name == "email":
                emails.extend([event.old_value, event.new_value])
            elif field_name == "phone":
                phones.extend([event.old_value, event.new_value])
            elif field_name == "email_hash":
                hashes.extend([event.old_value, event.new_value])

        current_email = record.email if record else None
        current_phone = record.phone if record else None
        current_hash = record.email_hash if record else None

        historical_emails = [
            value for value in _dedupe(emails)
            if normalise_email(value) != normalise_email(current_email)
        ]
        historical_phones = [
            value for value in _dedupe(phones)
            if normalise_phone(value) != normalise_phone(current_phone)
        ]
        historical_hashes = [
            value for value in _dedupe(hashes)
            if normalise_hash(value) != normalise_hash(current_hash)
        ]

        return IdentifierSet(
            user_id=user_id,
            current_email=current_email,
            current_phone=current_phone,
            current_email_hash=current_hash,
            historical_emails=historical_emails,
            historical_phones=historical_phones,
            historical_email_hashes=historical_hashes,
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def normalise_email(value: str) -> str:
        """Lower-case and strip an email so comparisons are case-insensitive."""
        return normalise_email(value)

    def match_any_identifier(self, value: str) -> List[ResolutionResult]:
        """Match a raw evidence value (email, hash, or phone) against all subjects."""
        self._ensure_loaded()
        results: List[ResolutionResult] = []
        seen: set = set()

        key = normalise_email(value)
        if key and key in self._email_to_user:
            user_id = self._email_to_user[key]
            if user_id not in seen:
                seen.add(user_id)
                results.append(self.resolve(value))

        digest = normalise_hash(value)
        if digest and len(digest) == 64 and digest in self._hash_to_user:
            user_id = self._hash_to_user[digest]
            if user_id not in seen:
                seen.add(user_id)
                results.append(
                    ResolutionResult(
                        user_id, "email_hash", value, self.identifiers_for(user_id)
                    )
                )

        phone = normalise_phone(value)
        if phone and len(phone) >= 7 and phone in self._phone_to_user:
            user_id = self._phone_to_user[phone]
            if user_id not in seen:
                seen.add(user_id)
                results.append(
                    ResolutionResult(user_id, "phone", value, self.identifiers_for(user_id))
                )

        return results

    # -- introspection -----------------------------------------------------

    @property
    def identity_count(self) -> int:
        """Number of current identity snapshots loaded."""
        return len(self._identities)

    @property
    def audit_event_count(self) -> int:
        """Number of historical identity changes loaded."""
        return len(self._events)

    def all_user_ids(self) -> List[str]:
        """Stable user references, sorted for deterministic iteration."""
        return sorted(self._identities)
