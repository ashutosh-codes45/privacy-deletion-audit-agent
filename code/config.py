"""Central configuration for the "Ghost Data Still Exists" deletion-audit agent.

This module is the single place where paths, the output contract, and the
model pricing constraints are declared. Every other module imports from here so
that a run behaves identically on macOS, Linux, and Windows: all paths are
resolved relative to this file, never relative to the current working directory
or the user's home directory (AGENTS.md sections 6.7 and 8).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Dict, Tuple

# ---------------------------------------------------------------------------
# 1. Paths (all derived from this file's location, never hardcoded)
# ---------------------------------------------------------------------------

CODE_DIR: Path = Path(__file__).resolve().parent
REPO_ROOT: Path = CODE_DIR.parent
DATASET_DIR: Path = REPO_ROOT / "dataset"
EVIDENCE_DIR: Path = DATASET_DIR / "evidence"
EVALUATION_DIR: Path = CODE_DIR / "evaluation"

# Participant-facing inputs.
DELETION_REQUESTS_CSV: Path = DATASET_DIR / "deletion_requests.csv"
USER_IDENTITIES_CSV: Path = DATASET_DIR / "user_identities.csv"
USER_AUDIT_LOG_CSV: Path = DATASET_DIR / "user_audit_log.csv"
SYSTEM_ACTIVITY_CSV: Path = DATASET_DIR / "system_activity.csv"
DELETION_RULES_CSV: Path = DATASET_DIR / "deletion_rules.csv"
EVIDENCE_FILES_CSV: Path = DATASET_DIR / "evidence_files.csv"

# Public sample inputs used for offline scoring (32 cases with known answers).
SAMPLE_REQUESTS_CSV: Path = DATASET_DIR / "sample_deletion_requests.csv"
SAMPLE_OUTPUT_CSV: Path = DATASET_DIR / "sample_output.csv"

# Artifacts the solution must produce.
OUTPUT_CSV: Path = REPO_ROOT / "output.csv"
USAGE_REPORT_MD: Path = EVALUATION_DIR / "usage_report.md"
EVALUATION_REPORT_MD: Path = EVALUATION_DIR / "evaluation_report.md"

# ---------------------------------------------------------------------------
# 2. I/O conventions
# ---------------------------------------------------------------------------

CSV_ENCODING: str = "utf-8"
CSV_NEWLINE: str = "\n"
JSON_DUMP_SEPARATORS: Tuple[str, str] = (",", ":")

# AGENTS.md section 6.1: deletion_requests.csv holds 518 evaluation requests.
EXPECTED_CASE_COUNT: int = 518

# ---------------------------------------------------------------------------
# 3. Output contract (AGENTS.md sections 6.3 and 6.4)
# ---------------------------------------------------------------------------

OUTPUT_COLUMNS: Tuple[str, ...] = ("case_id", "deletion_status", "steps_json")

DELETION_STATUSES: Tuple[str, ...] = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")

PHASES: Tuple[str, ...] = ("FINDING", "REMEDIATION", "VERIFICATION")

STEP_FIELDS: Tuple[str, ...] = (
    "step_no",
    "phase",
    "system_id",
    "resource_id",
    "operation",
    "state",
    "evidence_id",
)

# Every operation is legal in exactly one phase, and carries exactly one state.
OPERATION_STATE_BY_PHASE: Dict[str, Dict[str, str]] = {
    "FINDING": {
        "REPORT_REMAINING_RECORD": "PRESENT",
        "REPORT_EXPOSED_PII": "PRESENT",
        "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
        "REPORT_RESTORE_RISK": "ACTIVE",
    },
    "REMEDIATION": {
        "DISABLE_DATA_FLOW": "DISABLED",
        "BLOCK_RESTORE": "BLOCKED",
        "DELETE_RECORD": "ABSENT",
        "REDACT_RECORD": "REDACTED",
        "ANONYMIZE_RECORD": "ANONYMIZED",
    },
    "VERIFICATION": {
        "VERIFY_RECORD_ABSENT": "ABSENT",
        "VERIFY_RECORD_PRESENT": "PRESENT",
        "VERIFY_RECORD_REDACTED": "REDACTED",
        "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
        "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
        "VERIFY_RESTORE_BLOCKED": "BLOCKED",
    },
}

# Resource-id prefix to owning system, observed across the evidence corpus.
SYSTEM_BY_RESOURCE_PREFIX: Dict[str, str] = {
    "account_": "account_service",
    "search_": "search_service",
    "analytics_": "analytics_platform",
    "support_": "support_platform",
    "export_": "export_service",
    "backup_": "backup_service",
    "data_flow_": "integration_service",
    "audit_record_": "security_service",
    "tombstone_": "identity_service",
    "cache_": "cache_service",
    "email_delivery_": "email_delivery_service",
    "legal_hold_": "legal_service",
    "marketing_": "marketing_platform",
    "object_": "object_store",
}

# ---------------------------------------------------------------------------
# 4. Identity resolution
# ---------------------------------------------------------------------------

# The request email may be a current or a historical address; the audit log can
# require several hops to reach the live identity (Phase 1, Task C, case_10).
MAX_IDENTITY_HOPS: int = 8

# ---------------------------------------------------------------------------
# 5. Model pricing constraints
# ---------------------------------------------------------------------------
#
# IMPORTANT: the numbers below are PLACEHOLDERS kept so that cost arithmetic is
# exercised end to end. Replace them with your provider's currently published
# list prices before trusting any figure in usage_report.md, either by editing
# this table or by exporting an override:
#
#   ORCHESTRATE_PRICING_JSON='{"DeepSeek-V4.1-Flash": {"provider": "deepseek",
#       "input_per_mtok": 0.28, "output_per_mtok": 0.42, "verified": true}}'
#
# Rates are US dollars per one million tokens, which is how providers publish
# them; the tracker converts to a per-token rate internally.

PRICING_ENV_VAR: str = "ORCHESTRATE_PRICING_JSON"
PRICING_CURRENCY: str = "USD"

DEFAULT_PROVIDER: str = "deepseek"
DEFAULT_MODEL: str = "DeepSeek-V4.1-Flash"

PRICING_IS_PLACEHOLDER: bool = False

PRICING_TABLE: Dict[str, Dict[str, object]] = {
    "DeepSeek-V4.1-Flash": {
        "provider": DEFAULT_PROVIDER,
        "input_per_mtok": 0.14,
        "output_per_mtok": 0.28,
        "verified": True,
    },
    "DEFAULT": {
        "provider": "unknown",
        "input_per_mtok": 0.14,
        "output_per_mtok": 0.28,
        "verified": False,
    },
}


# ---------------------------------------------------------------------------
# 6. Multimodal evidence extraction (Phase 3)
# ---------------------------------------------------------------------------
#
# PDF documents and image captures are read once each by ``multimodal.py`` and
# memoised in a local content cache, so an asset is never paid for twice. The
# vision credential is read from the environment only (AGENTS.md 4.4 and 6.7);
# with no credential configured, images stay unresolved and contribute no facts.

LLM_API_KEY_ENV: str = "ORCHESTRATE_LLM_API_KEY"
LLM_BASE_URL_ENV: str = "ORCHESTRATE_LLM_BASE_URL"
VISION_MODEL_ENV: str = "ORCHESTRATE_VISION_MODEL"
DEFAULT_VISION_BASE_URL: str = "https://api.deepseek.com/v1"
DEFAULT_VISION_MODEL: str = DEFAULT_MODEL

# Local extraction cache: keyed by (file_id, sha256 of the asset). It is a
# throwaway build artefact, so it lives in the system temp directory rather than
# in the repository (AGENTS.md section 6.9 excludes caches from the submission).
EVIDENCE_CACHE_JSON: Path = Path(tempfile.gettempdir()) / "orchestrate_evidence_cache.json"

# ---------------------------------------------------------------------------
# 7. Identifier conventions observed in the account snapshots (Phase 3)
# ---------------------------------------------------------------------------
#
# In the failover-replica captures the account resource id embeds the subject's
# stable internal key: ``account_<user number zero padded to 3>`` belongs to
# ``user_<user number>`` (so ``account_005`` <-> ``user_05``, and the padding
# disappears above 999, ``account_8741`` <-> ``user_8741``). The alignment is a
# property of that capture, not a universal rule: a snapshot holding a row that
# contradicts it must win. It is therefore applied only when the snapshot itself
# demonstrates the alignment, and only when no account resource has already been
# evidenced for the subject from an explicit identifier match.

ACCOUNT_ID_PREFIX: str = "account"
ACCOUNT_ID_MIN_WIDTH: int = 3
REPLICA_MARKER: str = "replica"

# System prompt for the vision / document extraction hook. It carries the
# indirect-reference instruction (AGENTS.md 6.6) and the identifier convention
# above so that the model resolves hidden naming alignments instead of guessing.
EXTRACTION_SYSTEM_PROMPT: str = """\
You are the evidence-extraction component of a deletion-audit agent for a \
commerce platform. You read ONE evidence artefact (an application screenshot, \
a scan of an internal screen, or a document image) and report only facts that \
are literally visible in it.

Report, for every record you can see:
* resource_id - the exact resource identifier printed, e.g. account_001, \
search_428, support_001, data_flow_1121, backup_4978.
* system_id - the owning system, when the artefact names one.
* identifier - any email address, phone number, e-mail hash or stable reference \
printed next to the record (user_07, ticket #8942, order ORD-20264118).
* state - the record state as printed (PRESENT, ABSENT, DELETED, REDACTED, \
ANONYMIZED, BLOCKED, DISABLED, ACTIVE, ...).
* restore_allowed, enabled - the literal boolean when the artefact shows one.
* note - one short sentence of visible context.

Indirect references
-------------------
Artefacts frequently name a person indirectly, for example "the user \
associated with ticket #8942" or "customer of order ORD-20264118". Resolve such \
a phrase to the actual subject only by matching identifiers you can read \
(email, phone, e-mail hash, user_NN reference) against the identifier lookup \
index supplied with the request. If the artefact does not let you resolve the \
reference, report the phrase verbatim in `identifier` and write `unresolved` in \
`note`. Never infer an identity the artefact does not support.

Identifier conventions observed in this platform
------------------------------------------------
In the failover-replica account captures the account resource id embeds the \
subject's stable internal key: the padded number in account_00N belongs to the \
same subject as user_0N, and the padding disappears above 999 (account_8741 \
belongs to user_8741). Apply this alignment only when the snapshot you are \
reading shows it itself - a row pairing that resource_id with that user_id, or \
a primary/replica conflict for that subject. Never apply it to a record whose \
own row contradicts it.

Output
------
Return a single JSON array and nothing else, one object per record:
[{"resource_id": "...", "system_id": "...", "identifier": "...", \
"state": "...", "note": "..."}]
Omit any key you cannot read. Return [] when the artefact shows no record. \
Never invent an identifier, a resource or a state.
"""


def load_pricing_table() -> Dict[str, Dict[str, object]]:
    """Return the effective pricing table, honouring the env override.

    A malformed override is ignored rather than fatal: a full dataset sweep must
    still complete even if pricing metadata is wrong (AGENTS.md section 6.7).
    """
    table: Dict[str, Dict[str, object]] = {
        name: dict(rates) for name, rates in PRICING_TABLE.items()
    }
    raw = os.environ.get(PRICING_ENV_VAR, "").strip()
    if not raw:
        return table
    try:
        override = json.loads(raw)
    except (ValueError, TypeError):
        return table
    if not isinstance(override, dict):
        return table
    for model_name, rates in override.items():
        if isinstance(rates, dict):
            merged = dict(table.get(model_name, table["DEFAULT"]))
            merged.update(rates)
            table[model_name] = merged
    return table


def pricing_for(model: str, table: Dict[str, Dict[str, object]] | None = None) -> Dict[str, object]:
    """Look up the pricing entry for ``model``, falling back to ``"DEFAULT"``."""
    effective = table if table is not None else load_pricing_table()
    entry = effective.get(model)
    if entry is None:
        entry = dict(effective.get("DEFAULT", PRICING_TABLE["DEFAULT"]))
        entry["provider"] = str(entry.get("provider", "unknown"))
    return dict(entry)
