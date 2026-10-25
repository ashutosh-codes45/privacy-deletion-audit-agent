"""Deletion-audit engine: case orchestration and output serialization.

Owns the per-case decision pipeline and the exact serialization of
``output.csv``:

    request -> authorized? -> resolve identifiers -> reconstruct state at
    ``audit_at`` -> apply deletion_rules.csv -> FINDING / REMEDIATION /
    VERIFICATION steps -> one output row per ``case_id``

Contract invariants enforced here (Phase 1 Task C, AGENTS.md section 6):

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

Phase 2 status
--------------
This module implements the deterministic baseline:

1. multilingual ``request_text`` triage -> ``NOT_APPLICABLE`` + ``[]``;
2. identity resolution delegated to :mod:`identity_resolver`;
3. a deterministic scan of the *text* evidence corpus and of
   ``system_activity.csv`` to locate the resources that still hold the subject's
   data, mapped onto the allowed FINDING / REMEDIATION / VERIFICATION steps.

No LLM or vision hooks are present: image and PDF evidence is enumerated in the
manifest but its pixels/typography are not read by this baseline.
"""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:  # imported as part of the ``code`` package
    import config
    import identity_resolver as identity
except ImportError:  # pragma: no cover - direct script execution
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config
    import identity_resolver as identity


# ---------------------------------------------------------------------------
# Resource vocabulary
# ---------------------------------------------------------------------------

# resource-id prefix -> (owning system, deletion_rules data category, action).
# ``DATA_FLOW`` has no row in deletion_rules.csv: an active connector is a
# recreation risk rather than a storable record, and is handled as a flow.
RESOURCE_KINDS: Dict[str, Tuple[str, str, str]] = {
    "account": ("account_service", "USER_PROFILE", "DELETE"),
    "search": ("search_service", "SEARCH_INDEX_ENTRY", "DELETE"),
    "analytics": ("analytics_platform", "ANALYTICS_PROFILE", "ANONYMIZE"),
    "support": ("support_platform", "SUPPORT_MESSAGE", "REDACT"),
    "export": ("export_service", "USER_EXPORT", "DELETE"),
    "backup": ("backup_service", "BACKUP_ARCHIVE", "RETAIN"),
    "data_flow": ("integration_service", "DATA_FLOW", "DISABLE_FLOW"),
    "audit_record": ("security_service", "SECURITY_AUDIT_LOG", "RETAIN"),
    "tombstone": ("identity_service", "ACCOUNT_TOMBSTONE", "REDACT"),
    "cache": ("cache_service", "CACHE_ENTRY", "DELETE"),
    "email_delivery": ("email_delivery_service", "EMAIL_DELIVERY_LOG", "REDACT"),
    "legal_hold": ("legal_service", "LEGAL_HOLD_RECORD", "RETAIN"),
    "marketing": ("marketing_platform", "MARKETING_PROFILE", "DELETE"),
    "object": ("object_store", "SUPPORT_ATTACHMENT", "DELETE"),
}

_RESOURCE_RE = re.compile(
    r"\b(" + "|".join(sorted(RESOURCE_KINDS, key=len, reverse=True)) + r")_(\d+)\b"
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")
_PHONE_RE = re.compile(r"\+\d[\d\s\-]{7,}\d")
_REF_RE = re.compile(r"\buser_\d+\b")

TEXT_EVIDENCE_SUFFIXES = frozenset({".sql", ".log", ".json", ".txt", ".csv", ".ndjson", ".md"})
MAX_EVIDENCE_BYTES = 12 * 1024 * 1024
MAX_CONTEXT_CHARS = 400


# ---------------------------------------------------------------------------
# Request-text triage (multilingual, deterministic)
# ---------------------------------------------------------------------------

# Phrases that explicitly *withhold* deletion consent. Checked first so that
# "please don't delete" never reads as a deletion order.
NON_DELETION_PHRASES: Tuple[str, ...] = (
    "not asking for deletion",
    "not a request to erase",
    "not a request to delete",
    "do not close the account",
    "don't close the account",
    "do not close my account",
    "do not delete",
    "don't delete",
    "please don't delete",
    "keep the account",
    "keep the account as it is",
    "keep my account",
    "leave the account active",
    "leave the account",
    "leave my profile",
    "leave my profile and purchase history",
    "without removing",
    "continue using the service",
    "still need the account",
    "i still need the account",
    "still use it",
    "remove me from marketing lists only",
    "stop the promotional emails",
    "information request",
    "copy of the information held on my account",
    "copy of last year's invoices",
    "download my account information",
    "resetting the login",
    "change the email address used for this account",
    "replace it with the new one",
)

# Stems/phrases that *do* authorise deletion, across the request languages seen
# in the corpus (English, Spanish, French, Portuguese, Japanese, Korean).
DELETION_KEYWORDS: Tuple[str, ...] = (
    "delet",          # delete / deleted / deleting / deletion
    "erase",
    "erasure",
    "erased",
    "remove",         # negation phrases are matched before this list
    "close my account",
    "close this account",
    "close the account",
    "account closed",
    "permanent account deletion",
    "personal-data deletion",
    "personal data deleted",
    "profile is gone",
    "account is gone",
    "elimina",        # es/pt: eliminar, eliminación
    "borrar",
    "suprimir",
    "supresión",
    "supression",
    "suppress",
    "supprimer",
    "supprimez",
    "suppression",
    "effacer",
    "cerrar mi cuenta",
    "excluir",
    "apagar",
    "encerrar",
    "削除",
    "消去",
    "抹消",
    "삭제",
    "탈퇴",
)


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
        """Raise ``ValueError`` if the step violates AGENTS.md section 6.4."""
        if not isinstance(self.step_no, int) or self.step_no < 1:
            raise ValueError(f"step_no must be a positive integer, got {self.step_no!r}")
        if self.phase not in config.PHASES:
            raise ValueError(f"unknown phase {self.phase!r}")
        allowed = config.OPERATION_STATE_BY_PHASE[self.phase]
        if self.operation not in allowed:
            raise ValueError(f"operation {self.operation!r} is not legal in phase {self.phase!r}")
        expected_state = allowed[self.operation]
        if self.state != expected_state:
            raise ValueError(
                f"operation {self.operation!r} requires state {expected_state!r}, got {self.state!r}"
            )
        if not self.system_id or not self.resource_id:
            raise ValueError("system_id and resource_id must be non-empty")


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
        """Raise ``ValueError`` if the answer violates the output contract."""
        if self.deletion_status not in config.DELETION_STATUSES:
            raise ValueError(f"unknown deletion_status {self.deletion_status!r}")
        if self.deletion_status == "NOT_APPLICABLE":
            if self.steps:
                raise ValueError("NOT_APPLICABLE must serialize an empty step list")
            return
        for expected_no, step in enumerate(self.steps, start=1):
            if step.step_no != expected_no:
                raise ValueError(f"{self.case_id}: step_no {step.step_no} breaks numbering at {expected_no}")
            step.validate()
        rank = {phase: index for index, phase in enumerate(config.PHASES)}
        ranks = [rank[step.phase] for step in self.steps]
        if ranks != sorted(ranks):
            raise ValueError(f"{self.case_id}: phases are out of order")
        if self.deletion_status == "COMPLETE":
            for step in self.steps:
                if step.phase != "VERIFICATION":
                    raise ValueError(f"{self.case_id}: COMPLETE may only contain VERIFICATION steps")
                if not step.evidence_id:
                    raise ValueError(f"{self.case_id}: COMPLETE verification must cite evidence")


def serialize_steps(steps: Sequence[AuditStep]) -> str:
    """Encode steps as deterministic JSON: no whitespace drift, array order kept."""
    return json.dumps(
        [step.to_dict() for step in steps],
        separators=config.JSON_DUMP_SEPARATORS,
        ensure_ascii=False,
    )


@dataclass(frozen=True)
class ResourceMention:
    """A resource that a subject's identifier is still attached to."""

    resource_id: str
    evidence_id: str
    captured_at: str
    context: str

    @property
    def prefix(self) -> str:
        return self.resource_id.rsplit("_", 1)[0]

    @property
    def system_id(self) -> str:
        return RESOURCE_KINDS[self.prefix][0]

    @property
    def category(self) -> str:
        return RESOURCE_KINDS[self.prefix][1]

    @property
    def action(self) -> str:
        return RESOURCE_KINDS[self.prefix][2]


@dataclass(frozen=True)
class ResolvedResource:
    """A mention after the policy treatment has been decided."""

    mention: ResourceMention
    compliant: bool
    finding_operation: Optional[str] = None
    remediation_operation: Optional[str] = None
    verification_operation: str = ""
    state_observed: str = ""


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------


def _flat_text(value: Any) -> str:
    return str(value)


def _extract_keys(text: str) -> Dict[str, set]:
    """Return the identifier keys a chunk of text carries."""
    lowered = text.lower()
    return {
        "email": {match.group(0).lower() for match in _EMAIL_RE.finditer(text)},
        "hash": {match.group(0).lower() for match in _HEX_RE.finditer(text)},
        "phone": {
            "".join(ch for ch in match.group(0) if ch.isdigit())
            for match in _PHONE_RE.finditer(text)
        },
        "ref": {match.group(0).lower() for match in _REF_RE.finditer(lowered)},
    }


def _extract_resources(text: str) -> List[str]:
    """Return every known resource id mentioned in ``text`` (order preserved)."""
    seen: List[str] = []
    for match in _RESOURCE_RE.finditer(text):
        rid = match.group(0)
        if rid not in seen:
            seen.append(rid)
    return seen


def _record_scalars(node: Dict[str, Any], sink: Dict[str, Any]) -> None:
    """Flatten a JSON dict's scalar fields, descending into nested dicts only."""
    for key, value in node.items():
        if isinstance(value, dict):
            _record_scalars(value, sink)
        elif isinstance(value, list):
            continue
        elif key not in sink:
            sink[key] = value


def _iter_json_records(node: Any) -> Iterable[Dict[str, Any]]:
    """Yield one flattened scalar map per JSON dict node.

    Lists are descended into separately so that a container of many records
    never cross-links one record's identifier to another record's resource.
    """
    if isinstance(node, dict):
        sink: Dict[str, Any] = {}
        _record_scalars(node, sink)
        yield sink
        for value in node.values():
            if isinstance(value, list):
                for item in value:
                    yield from _iter_json_records(item)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_json_records(item)


def _context_from_mapping(mapping: Dict[str, Any]) -> str:
    parts = [f"{key}={_flat_text(value)}" for key, value in sorted(mapping.items())]
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Evidence corpus
# ---------------------------------------------------------------------------


class EvidenceCorpus:
    """The manifest plus a reverse index from identifier key to resource mentions."""

    def __init__(self, manifest_csv: Path = config.EVIDENCE_FILES_CSV) -> None:
        self.manifest_csv = Path(manifest_csv)
        self.files: Dict[str, Dict[str, str]] = {}
        self._index: Dict[Tuple[str, str], List[ResourceMention]] = defaultdict(list)
        self._loaded = False
        self.readable_files = 0
        self.skipped_files: List[str] = []

    # -- loading -----------------------------------------------------------

    def load(self) -> "EvidenceCorpus":
        with open(self.manifest_csv, "r", encoding=config.CSV_ENCODING, newline="") as fh:
            for row in csv.DictReader(fh):
                file_id = (row.get("file_id") or "").strip()
                if not file_id:
                    continue
                self.files[file_id] = {
                    "file_path": (row.get("file_path") or "").strip(),
                    "origin": (row.get("origin") or "").strip(),
                    "captured_at": (row.get("captured_at") or "").strip(),
                }

        for file_id in sorted(self.files):
            meta = self.files[file_id]
            path = config.DATASET_DIR / meta["file_path"]
            if path.suffix.lower() not in TEXT_EVIDENCE_SUFFIXES:
                self.skipped_files.append(file_id)
                continue
            if not path.is_file() or path.stat().st_size > MAX_EVIDENCE_BYTES:
                self.skipped_files.append(file_id)
                continue
            try:
                text = path.read_text(encoding=config.CSV_ENCODING, errors="replace")
            except OSError:
                self.skipped_files.append(file_id)
                continue
            self.readable_files += 1
            self._ingest(text, path.suffix.lower(), file_id, meta["captured_at"])

        self._loaded = True
        return self

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _ingest(self, text: str, suffix: str, file_id: str, captured_at: str) -> None:
        if suffix == ".json":
            try:
                payload = json.loads(text)
            except (ValueError, TypeError):
                self._ingest_lines(text, file_id, captured_at)
                return
            for record in _iter_json_records(payload):
                resources = []
                for value in record.values():
                    resources.extend(_extract_resources(_flat_text(value)))
                if len(set(resources)) != 1:
                    continue
                self._emit(record, resources[0], file_id, captured_at)
            return
        self._ingest_lines(text, file_id, captured_at)

    def _ingest_lines(self, text: str, file_id: str, captured_at: str) -> None:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            resources = _extract_resources(line)
            if not resources:
                continue
            identifier_keys = _extract_keys(line)
            if not any(identifier_keys.values()):
                continue
            for resource_id in resources:
                self._emit(line, resource_id, file_id, captured_at)

    def _emit(self, source: Any, resource_id: str, file_id: str, captured_at: str) -> None:
        if isinstance(source, dict):
            keys = {"email": set(), "hash": set(), "phone": set(), "ref": set()}
            for value in source.values():
                found = _extract_keys(_flat_text(value))
                for name, values in found.items():
                    keys[name] |= values
            context = _context_from_mapping(source)
        else:
            keys = _extract_keys(str(source))
            context = str(source)
        context = context[:MAX_CONTEXT_CHARS]
        mention = ResourceMention(resource_id, file_id, captured_at, context)
        for name, values in keys.items():
            for value in values:
                self._index[(name, value)].append(mention)

    # -- queries -----------------------------------------------------------

    @staticmethod
    def _admissible(captured_at: str, at: Optional[str]) -> bool:
        return identity._is_admissible(captured_at, at)

    def mentions_for(self, keys: Iterable[Tuple[str, str]], at: Optional[str]) -> Dict[str, ResourceMention]:
        """Best admissible mention per resource for the supplied identifier keys."""
        self._ensure_loaded()
        collected: Dict[str, List[ResourceMention]] = defaultdict(list)
        for key in keys:
            for mention in self._index.get(key, ()):
                if not self._admissible(mention.captured_at, at):
                    continue
                collected[mention.resource_id].append(mention)

        best: Dict[str, ResourceMention] = {}
        for resource_id, mentions in collected.items():
            # Most recent admissible proof wins; file_id breaks ties deterministically.
            best[resource_id] = max(mentions, key=lambda m: (m.captured_at, m.evidence_id))
        return best


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------


class ActivityLog:
    """Deterministic scanner over ``system_activity.csv``.

    Two jobs:
    * recover identity links (``LINK_RESOURCE_IDENTITY`` rows name the email and
      stable user reference a resource belongs to);
    * describe the state events for a subject's resources: failures, index
      updates, data flows, and restore/checkpoint probes.
    """

    FAILURE_RESULTS = ("FAILED", "PARTIAL")

    def __init__(self, activity_csv: Path = config.SYSTEM_ACTIVITY_CSV) -> None:
        self.activity_csv = Path(activity_csv)
        self.rows: List[Dict[str, str]] = []
        self._by_resource: Dict[str, List[Dict[str, str]]] = defaultdict(list)
        self._loaded = False

    def load(self) -> "ActivityLog":
        with open(self.activity_csv, "r", encoding=config.CSV_ENCODING, newline="") as fh:
            for row in csv.DictReader(fh):
                clean = {key: (value or "").strip() for key, value in row.items()}
                self.rows.append(clean)
                resource_id = clean.get("resource_id", "")
                if resource_id:
                    self._by_resource[resource_id].append(clean)
        self._loaded = True
        return self

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def links_for(
        self,
        keys: Iterable[Tuple[str, str]],
        at: Optional[str],
    ) -> List[Tuple[str, str, str, str]]:
        """Return ``(resource_id, activity_id, captured_at, details)`` identity links."""
        self._ensure_loaded()
        wanted = set(keys)
        if not wanted:
            return []
        links: List[Tuple[str, str, str, str]] = []
        for row in self.rows:
            if row.get("event_type") != "LINK_RESOURCE_IDENTITY":
                continue
            details = row.get("details", "")
            found = _extract_keys(details)
            matched = any((name, value) in wanted for name, values in found.items() for value in values)
            if not matched:
                continue
            event_time = row.get("event_time", "")
            if not identity._is_admissible(event_time, at):
                continue
            resource_id = row.get("resource_id", "")
            if resource_id:
                links.append((resource_id, row.get("activity_id", ""), event_time, details))
        return links

    def events_for(
        self,
        resource_ids: Iterable[str],
        at: Optional[str],
    ) -> Dict[str, List[Dict[str, str]]]:
        """Group the state events of ``resource_ids`` into four buckets."""
        self._ensure_loaded()
        buckets: Dict[str, List[Dict[str, str]]] = {
            "failure": [],
            "index": [],
            "data_flow": [],
            "restore": [],
        }
        for resource_id in sorted(set(resource_ids)):
            for row in self._by_resource.get(resource_id, ()):
                if not identity._is_admissible(row.get("event_time", ""), at):
                    continue
                event_type = row.get("event_type", "")
                if (
                    row.get("reported_result") in self.FAILURE_RESULTS
                    and event_type
                    in ("DELETE_RECORD", "UPDATE_RECORD", "INDEX_RECORD", "RETAIN_RECORD", "SYNC_RECORD")
                ):
                    buckets["failure"].append(row)
                if event_type in ("INDEX_RECORD", "UPDATE_RECORD"):
                    buckets["index"].append(row)
                if event_type == "SYNC_RECORD":
                    buckets["data_flow"].append(row)
                if event_type in ("CHECK_RESTORE_ACCESS", "CHECK_DATA_FLOW"):
                    buckets["restore"].append(row)
        return buckets


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    rule_id: str
    data_category: str
    condition: str
    required_action: str


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

_FINDING_RANK = {
    "REPORT_REMAINING_RECORD": 0,
    "REPORT_EXPOSED_PII": 1,
    "REPORT_ACTIVE_DATA_FLOW": 2,
    "REPORT_RESTORE_RISK": 3,
}
_REMEDIATION_RANK = {
    "DISABLE_DATA_FLOW": 0,
    "BLOCK_RESTORE": 1,
    "DELETE_RECORD": 2,
    "REDACT_RECORD": 3,
    "ANONYMIZE_RECORD": 4,
}
_VERIFICATION_RANK = {
    "VERIFY_RECORD_ABSENT": 0,
    "VERIFY_RECORD_PRESENT": 1,
    "VERIFY_RECORD_REDACTED": 2,
    "VERIFY_RECORD_ANONYMIZED": 3,
    "VERIFY_DATA_FLOW_DISABLED": 4,
    "VERIFY_RESTORE_BLOCKED": 5,
}

_ACTION_TO_FINDING = {
    "DELETE": "REPORT_REMAINING_RECORD",
    "REDACT": "REPORT_EXPOSED_PII",
    "ANONYMIZE": "REPORT_EXPOSED_PII",
}
_ACTION_TO_REMEDIATION = {
    "DELETE": "DELETE_RECORD",
    "REDACT": "REDACT_RECORD",
    "ANONYMIZE": "ANONYMIZE_RECORD",
}
_ACTION_TO_VERIFICATION = {
    "DELETE": "VERIFY_RECORD_ABSENT",
    "REDACT": "VERIFY_RECORD_REDACTED",
    "ANONYMIZE": "VERIFY_RECORD_ANONYMIZED",
    "RETAIN": "VERIFY_RECORD_PRESENT",
}


def _flag(context: str, token: str) -> bool:
    return token in context.lower()


class DeletionAuditEngine:
    """Decide one case at a time and emit the full ``output.csv``."""

    def __init__(
        self,
        rules_csv: Path = config.DELETION_RULES_CSV,
        evidence_manifest_csv: Path = config.EVIDENCE_FILES_CSV,
        activity_csv: Path = config.SYSTEM_ACTIVITY_CSV,
        identities_csv: Path = config.USER_IDENTITIES_CSV,
        audit_log_csv: Path = config.USER_AUDIT_LOG_CSV,
    ) -> None:
        self.rules_csv = Path(rules_csv)
        self.evidence_manifest_csv = Path(evidence_manifest_csv)
        self.activity_csv = Path(activity_csv)
        self._rules: List[Rule] = []
        self._action_by_category: Dict[str, str] = {}
        self._evidence_index: Dict[str, Dict[str, str]] = {}
        self.resolver = identity.IdentityResolver(identities_csv, audit_log_csv)
        self.corpus = EvidenceCorpus(self.evidence_manifest_csv)
        self.activity = ActivityLog(self.activity_csv)
        self._loaded = False

    # -- setup -------------------------------------------------------------

    def load_policy(self) -> None:
        """Load ``deletion_rules.csv``, the evidence manifest, identities, and activity."""
        with open(self.rules_csv, "r", encoding=config.CSV_ENCODING, newline="") as fh:
            for row in csv.DictReader(fh):
                rule = Rule(
                    rule_id=(row.get("rule_id") or "").strip(),
                    data_category=(row.get("data_category") or "").strip(),
                    condition=(row.get("condition") or "").strip(),
                    required_action=(row.get("required_action") or "").strip().upper(),
                )
                if not rule.data_category or not rule.required_action:
                    continue
                self._rules.append(rule)
                self._action_by_category.setdefault(rule.data_category, rule.required_action)

        self._evidence_index = dict(self.corpus.files) if self.corpus.files else {}
        self.resolver.load()
        self.corpus.load()
        self.activity.load()
        self._evidence_index = dict(self.corpus.files)
        self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load_policy()

    # -- policy lookup -----------------------------------------------------

    @property
    def rules(self) -> List[Rule]:
        return list(self._rules)

    def action_for_category(self, category: str) -> str:
        """Required action for ``category``, falling back to the record's own kind."""
        return self._action_by_category.get(category, "DELETE")

    # -- per-case pipeline -------------------------------------------------

    def authorizes_deletion(self, request_text: str) -> bool:
        """Decide whether the request text actually authorizes deletion."""
        text = (request_text or "").strip().lower()
        if not text:
            return False
        for phrase in NON_DELETION_PHRASES:
            if phrase in text:
                return False
        return any(keyword in text for keyword in DELETION_KEYWORDS)

    def _subject_keys(self, request: Dict[str, str], at: Optional[str]) -> Tuple[Optional[str], set]:
        """Resolve the request email and assemble every identifier key."""
        resolution = self.resolver.resolve(request.get("email", ""), at)
        user_id = resolution.matched_user_id
        keys: set = set()
        if resolution.identifiers is not None:
            search = resolution.identifiers.as_search_keys()
            keys |= {("email", value) for value in search["emails"]}
            keys |= {("hash", value) for value in search["hashes"]}
            keys |= {("phone", value) for value in search["phones"]}
            keys |= {("ref", value) for value in search["refs"]}
        if user_id:
            keys.add(("ref", user_id.strip().lower()))
        return user_id, keys

    def _collect_mentions(
        self,
        keys: set,
        at: Optional[str],
    ) -> Dict[str, ResourceMention]:
        """Union of corpus mentions and activity-log identity links.

        Activity-derived links only *fill gaps*: when a resource is already
        proven by a corpus file, that explicit artefact stays the cited
        ``evidence_id`` (AGENTS.md section 6.6).
        """
        mentions = self.corpus.mentions_for(keys, at)
        for resource_id, activity_id, event_time, details in self.activity.links_for(keys, at):
            mentions.setdefault(
                resource_id,
                ResourceMention(resource_id, activity_id, event_time, details[:MAX_CONTEXT_CHARS]),
            )
        return mentions

    def _resolve_resource(self, mention: ResourceMention) -> ResolvedResource:
        """Apply the policy action and the observed state to one resource."""
        prefix = mention.prefix
        if prefix not in RESOURCE_KINDS:
            return ResolvedResource(mention, compliant=True, verification_operation="")
        action = self.action_for_category(mention.category)
        context = mention.context
        absent = (
            _flag(context, "404")
            or _flag(context, "not_found")
            or _flag(context, "not found")
            or _flag(context, "rows=0")
            or _flag(context, "absent")
        )
        anonymized = _flag(context, "anonymized") or _flag(context, "no direct identifier")
        blocked = (
            _flag(context, "restore_allowed=false")
            or _flag(context, "restore_allowed=0")
            or _flag(context, "restore_allowed=false")
            or _flag(context, "blocked")
        )
        disabled = (
            _flag(context, "enabled=false")
            or _flag(context, "enabled=0")
            or _flag(context, "disabled")
            or _flag(context, "stopped")
            or _flag(context, "inactive")
        )
        redacted = _flag(context, "redacted") or _flag(context, "requester_email=null") or _flag(
            context, "requester_email=none"
        )

        if action == "DISABLE_FLOW":
            if disabled:
                return ResolvedResource(
                    mention, True, verification_operation="VERIFY_DATA_FLOW_DISABLED", state_observed="DISABLED"
                )
            return ResolvedResource(
                mention,
                False,
                finding_operation="REPORT_ACTIVE_DATA_FLOW",
                remediation_operation="DISABLE_DATA_FLOW",
                verification_operation="VERIFY_DATA_FLOW_DISABLED",
                state_observed="ACTIVE",
            )

        if mention.category == "BACKUP_ARCHIVE":
            if blocked:
                return ResolvedResource(
                    mention, True, verification_operation="VERIFY_RESTORE_BLOCKED", state_observed="BLOCKED"
                )
            return ResolvedResource(
                mention,
                False,
                finding_operation="REPORT_RESTORE_RISK",
                remediation_operation="BLOCK_RESTORE",
                verification_operation="VERIFY_RESTORE_BLOCKED",
                state_observed="ACTIVE",
            )

        if action == "RETAIN":
            if absent:
                return ResolvedResource(mention, True, verification_operation="", state_observed="ABSENT")
            return ResolvedResource(
                mention, True, verification_operation="VERIFY_RECORD_PRESENT", state_observed="PRESENT"
            )

        if action == "ANONYMIZE":
            if anonymized:
                return ResolvedResource(
                    mention, True, verification_operation="VERIFY_RECORD_ANONYMIZED", state_observed="ANONYMIZED"
                )
            return ResolvedResource(
                mention,
                False,
                finding_operation="REPORT_EXPOSED_PII",
                remediation_operation="ANONYMIZE_RECORD",
                verification_operation="VERIFY_RECORD_ANONYMIZED",
                state_observed="IDENTIFIABLE",
            )

        if action == "REDACT":
            if redacted:
                return ResolvedResource(
                    mention, True, verification_operation="VERIFY_RECORD_REDACTED", state_observed="REDACTED"
                )
            return ResolvedResource(
                mention,
                False,
                finding_operation="REPORT_EXPOSED_PII",
                remediation_operation="REDACT_RECORD",
                verification_operation="VERIFY_RECORD_REDACTED",
                state_observed="IDENTIFIABLE",
            )

        # DELETE
        if absent:
            return ResolvedResource(
                mention, True, verification_operation="VERIFY_RECORD_ABSENT", state_observed="ABSENT"
            )
        return ResolvedResource(
            mention,
            False,
            finding_operation="REPORT_REMAINING_RECORD",
            remediation_operation="DELETE_RECORD",
            verification_operation="VERIFY_RECORD_ABSENT",
            state_observed="PRESENT",
        )

    @staticmethod
    def _sort_key(resource: ResolvedResource) -> Tuple[str, str]:
        return (resource.mention.system_id, resource.mention.resource_id)

    def _build_steps(self, resources: Sequence[ResolvedResource]) -> Tuple[str, List[AuditStep]]:
        """Turn resolved resources into the ordered step list and a status."""
        findings = [r for r in resources if not r.compliant]
        status = "INCOMPLETE" if findings else "COMPLETE"

        ordered_findings = sorted(
            findings,
            key=lambda r: (
                _FINDING_RANK.get(r.finding_operation or "", 99),
                r.mention.system_id,
                r.mention.resource_id,
            ),
        )
        ordered_remediation = sorted(
            (r for r in findings if r.remediation_operation),
            key=lambda r: (
                _REMEDIATION_RANK.get(r.remediation_operation or "", 99),
                r.mention.system_id,
                r.mention.resource_id,
            ),
        )
        ordered_verification = sorted(
            (r for r in resources if r.verification_operation),
            key=lambda r: (
                _VERIFICATION_RANK.get(r.verification_operation or "", 99),
                0 if r.mention.system_id == "account_service" else 1,
                r.mention.system_id,
                r.mention.resource_id,
            ),
        )

        steps: List[AuditStep] = []
        counter = 1
        for resource in ordered_findings:
            steps.append(
                AuditStep(
                    step_no=counter,
                    phase="FINDING",
                    system_id=resource.mention.system_id,
                    resource_id=resource.mention.resource_id,
                    operation=resource.finding_operation or "",
                    state=config.OPERATION_STATE_BY_PHASE["FINDING"][resource.finding_operation or ""],
                    evidence_id=resource.mention.evidence_id,
                )
            )
            counter += 1
        for resource in ordered_remediation:
            steps.append(
                AuditStep(
                    step_no=counter,
                    phase="REMEDIATION",
                    system_id=resource.mention.system_id,
                    resource_id=resource.mention.resource_id,
                    operation=resource.remediation_operation or "",
                    state=config.OPERATION_STATE_BY_PHASE["REMEDIATION"][resource.remediation_operation or ""],
                    evidence_id="" if status == "INCOMPLETE" else resource.mention.evidence_id,
                )
            )
            counter += 1
        for resource in ordered_verification:
            steps.append(
                AuditStep(
                    step_no=counter,
                    phase="VERIFICATION",
                    system_id=resource.mention.system_id,
                    resource_id=resource.mention.resource_id,
                    operation=resource.verification_operation,
                    state=config.OPERATION_STATE_BY_PHASE["VERIFICATION"][resource.verification_operation],
                    evidence_id=resource.mention.evidence_id if status == "COMPLETE" else "",
                )
            )
            counter += 1
        return status, steps

    def run_case(self, request: Dict[str, str], tracker: Any = None) -> CaseAudit:
        """Produce the decided answer for one request row."""
        self._ensure_loaded()
        case_id = str(request.get("case_id", "")).strip()
        audit_at = str(request.get("audit_at", "")).strip() or None

        if not self.authorizes_deletion(request.get("request_text", "")):
            return CaseAudit(case_id, "NOT_APPLICABLE", [])

        _, keys = self._subject_keys(request, audit_at)
        mentions = self._collect_mentions(keys, audit_at)
        resources = [self._resolve_resource(mention) for mention in mentions.values()]
        status, steps = self._build_steps(resources)
        audit = CaseAudit(case_id, status, steps)
        audit.validate()
        return audit

    # -- sweep -------------------------------------------------------------

    def run_sweep(
        self,
        requests_csv: Path = config.DELETION_REQUESTS_CSV,
        output_csv: Path = config.OUTPUT_CSV,
        tracker: Any = None,
    ) -> List[CaseAudit]:
        """Audit every request and write one output row per ``case_id``."""
        self._ensure_loaded()
        with open(requests_csv, "r", encoding=config.CSV_ENCODING, newline="") as fh:
            requests = [dict(row) for row in csv.DictReader(fh)]

        results: List[CaseAudit] = []
        for request in requests:
            case_id = str(request.get("case_id", "")).strip()
            if tracker is None:
                results.append(self.run_case(request))
                continue
            with tracker.audit_block(case_id):
                results.append(self.run_case(request, tracker))

        self.write_output(results, output_csv)
        return results

    @staticmethod
    def write_output(results: Iterable[CaseAudit], path: Path = config.OUTPUT_CSV) -> Path:
        """Write ``output.csv`` with the contract columns, sorted by ``case_id``."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = sorted((result.to_row() for result in results), key=lambda row: _case_sort_key(row["case_id"]))
        with open(target, "w", encoding=config.CSV_ENCODING, newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(config.OUTPUT_COLUMNS), lineterminator=config.CSV_NEWLINE)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return target


def _case_sort_key(case_id: str) -> Tuple[int, str]:
    """Natural ``case_id`` ordering so ``case_100`` sorts after ``case_99``."""
    match = re.search(r"(\d+)$", case_id)
    if match:
        return (int(match.group(1)), case_id)
    return (1 << 30, case_id)


__all__ = [
    "AuditStep",
    "CaseAudit",
    "DeletionAuditEngine",
    "EvidenceCorpus",
    "ActivityLog",
    "ResourceMention",
    "ResolvedResource",
    "serialize_steps",
]
