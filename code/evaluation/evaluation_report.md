# Evaluation Report — Ghost Data Still Exists

Deletion-audit agent for HackerRank Orchestrate (October 2026).

- Solution entry point: `code/main.py` (deterministic engine in `code/audit_engine.py`)
- Scorer: `code/evaluation/local_scorer.py` (read-only; audits the 32 public samples)
- Sample set scored: `dataset/sample_deletion_requests.csv` against `dataset/sample_output.csv`
- Metric definitions: case-level status accuracy; per-step field precision/recall/F1 (aligned by
  `case_id` + `step_no`); exact step-sequence accuracy.

All numbers below are produced by:

```bash
python code/evaluation/local_scorer.py
python code/evaluation/local_scorer.py --show-mismatches   # per-case diff
```

---

## 1. Case-level (row) deletion-status accuracy

| Metric | Value |
| --- | ---: |
| Exact-match accuracy | **100.00%** (32/32) |
| Micro precision / recall / F1 | P 100.00% / R 100.00% / F1 100.00% |
| Macro precision / recall / F1 | P 100.00% / R 100.00% / F1 100.00% |

| Class | Support | TP | FP | FN | Precision | Recall | F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| COMPLETE | 10 | 10 | 0 | 0 | 100.00% | 100.00% | 100.00% |
| INCOMPLETE | 22 | 22 | 0 | 0 | 100.00% | 100.00% | 100.00% |
| NOT_APPLICABLE | 0 | 0 | 0 | 0 | — | — | — |

Note: the 32 public samples contain no `NOT_APPLICABLE` request, so that class is unexercised by
the sample set. It is covered by custom case C5 below.

## 2. Step-field accuracy (step-level)

| Metric | Value |
| --- | ---: |
| Field micro precision / recall / F1 | P 82.90% / R 80.75% / **F1 81.81%** |
| Field macro precision / recall / F1 | P 82.90% / R 80.75% / F1 81.81% |
| Steps: predicted vs expected | 188 vs 193 |

| Step field | Support | TP | FP | FN | Precision | Recall | F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `step_no` | 193 | 179 | 9 | 14 | 95.21% | 92.75% | 93.96% |
| `phase` | 193 | 170 | 18 | 23 | 90.43% | 88.08% | 89.24% |
| `system_id` | 193 | 151 | 37 | 42 | 80.32% | 78.24% | 79.27% |
| `resource_id` | 193 | 137 | 51 | 56 | 72.87% | 70.98% | 71.92% |
| `operation` | 193 | 140 | 48 | 53 | 74.47% | 72.54% | 73.49% |
| `state` | 193 | 146 | 42 | 47 | 77.66% | 75.65% | 76.64% |
| `evidence_id` | 193 | 168 | 20 | 25 | 89.36% | 87.05% | 88.19% |

## 3. Sequence accuracy (exact whole-array match)

| Metric | Value |
| --- | ---: |
| Exact step-sequence matches | **28.12%** (9/32) |

An exact sequence match requires the entire `steps_json` array for a case to equal the gold array
element-for-element after key-order normalisation. It is the strictest metric and is dominated by
the two weakest fields (`resource_id`, `operation`): a case with 8 correct steps and 1 wrong
`resource_id` still counts as a sequence miss.

## 4. Custom evaluation cases

Beyond the 32 provided samples I constructed five targeted cases that isolate the failure modes
found during the scorer loop. Each is expressed as a minimal evidence/request pair against the
real corpus and a stated expected decision.

### C1 — Split trace record: terminal status must beat the opening line

- Input: the paired lines in `evidence_94`
  (`... trace_id=rb-074950-001 event=profile_lookup_started resource_id=account_2064 ...` then
  `... trace_id=rb-074950-001 event=profile_lookup_completed resource_id=account_2064 rows=0 http_status=404`).
- Expected: the record is **ABSENT** (the completed read returned `rows=0 http_status=404`), so the
  case is `COMPLETE` and the finding cites `evidence_94`.
- Pre-fix behaviour: the mention tie-break used the latest timestamp and could surface the opening
  `profile_lookup_started` line, reporting a spurious `REPORT_REMAINING_RECORD`.
- Post-fix: `_context_weight` ranks the terminal line (`rows=`, `http_status=`, `404`) above the
  opening line (`_started`), independent of timestamp.

### C2 — Nested JSON service export must keep the true `resource_id`

- Input: `evidence_92`, `service_exports.account_service.profile_lookups[]` where a single parent
  object also carries sibling resource ids.
- Expected: the child record is indexed under its own `resource_id` (`account_6440` for
  `customer_reference=user_21`), not dropped and not re-keyed to a synthesised parent id.
- Pre-fix behaviour: `_iter_json_records` only descended direct list children, so nested
  `service_exports` records were discarded when the parent held several resource ids.
- Post-fix: the walker descends every dict and list value; JSON `null`/`true`/`false` are also
  rendered faithfully (`_flat_text`) so cleared identifier fields read as `email=null`.

### C3 — Tombstone with all direct identifiers cleared is REDACTED

- Input: `evidence_51`, record `tombstone_440` (`subject_reference=user_541`, `name: null`,
  `email: null`, `phone: null`, `email_hash: null`, `account_status: DELETED`).
- Expected: the tombstone is the policy-required artefact; its direct identifiers are removed, so
  the state is **REDACTED/ANONYMIZED** and the case is `COMPLETE` (matching case_31 rules), with the
  stable `subject_reference` preserved.
- Pre-fix behaviour: `str(None)` produced the literal `"None"`, so the `=null` markers were never
  recognised and the tombstone was reported as remaining PII.
- Post-fix: `_direct_identifiers_cleared()` flags the row when every present direct-identifier
  field is null/empty, and `redacted`/`anonymized` accept that flag.

### C4 — Active connector is a data flow, not a record to delete

- Input: `data_flow_6244` / `data_flow_2295` / `data_flow_2337` (cases 26/27/28 family), active
  with `enabled=true`.
- Expected: `REPORT_ACTIVE_DATA_FLOW` → `DISABLE_DATA_FLOW` → `VERIFY_DATA_FLOW_DISABLED`.
- Pre-fix behaviour: `deletion_rules.csv` has no `DATA_FLOW` row, so the hardcoded `"DELETE"`
  fallback classified the connector as a record and emitted `REPORT_REMAINING_RECORD`, flipping the
  cases to a wrong `INCOMPLETE`.
- Post-fix: `action_for_category(category, default)` is parameterised and called with the resource
  catalogue's own action, so a flow is judged as a flow.

### C5 — Non-deletion request must be `NOT_APPLICABLE`

- Input: `request_text` asking for access/export or a marketing opt-out rather than erasure.
- Expected: `NOT_APPLICABLE` with `steps_json=[]`, regardless of how the other columns look.
- Purpose: exercises the one class the 32 samples never cover, and guards the triage order
  (non-deletion consent-withholding phrases checked before the deletion-keyword list).

## 5. Failures found and improvements made

| # | Failure observed | Root cause | Fix (module) | Effect |
| --- | --- | --- | --- | --- |
| 1 | Nested JSON export records dropped / mis-keyed | `_iter_json_records` descended only direct list children | descend every dict/list value (`audit_engine.py`) | real ids captured (C2) |
| 2 | `email=null` never matched | `str(None)` → `"None"` | `_flat_text` renders `null`/`true`/`false` (`audit_engine.py`) | tombstone/redaction detection (C3) |
| 3 | Split log records lost the subject | closing line carried outcome but no identifier | `trace_keys` keyed by shared `trace_id` (`_ingest_lines`) | `support_002`, `export_7364`, `search_898` recovered (C1) |
| 4 | Opening trace line won the mention tie-break | `max()` on timestamp alone | structural `_context_weight` precedes timestamp in `mentions_for` | terminal status wins (C1) |
| 5 | Tombstone not recognised as REDACTED | no cleared-identifier rule | `_direct_identifiers_cleared()` wired into `redacted`/`anonymized` | case_31-style cases correct (C3) |
| 6 | Data flows judged as records to delete | `DATA_FLOW` absent from `deletion_rules.csv`; hardcoded `DELETE` fallback | parameterised `action_for_category(category, default)` and pass `mention.action` | cases 26/27/28 statuses corrected (C4) |
| 7 | COMPLETE rows with no verification | contract check iterated only existing steps | `CaseAudit.validate(admissible_evidence)` requires ≥1 admissible VERIFICATION step; `run_case` downgrades otherwise | COMPLETE now evidence-backed |

Scorer progression across the phases:

| Phase | Status accuracy | Exact sequence | Field micro F1 |
| --- | ---: | ---: | ---: |
| 2 (baseline) | 75.00% | 9.38% | 49.33% |
| 3 (multimodal hooks) | 75.00% | 18.75% | 56.00% |
| 4 (+ fixes 1–7) | **100.00%** | **28.12%** | **81.81%** |

## 6. Known residual gaps (not yet addressed)

- 188 predicted vs 193 expected steps: five expected steps are not emitted, concentrated in the
  larger multi-resource cases (e.g. case_17, case_18, case_29, case_30).
- Weakest fields remain `resource_id` (71.92%) and `operation` (73.49%): the engine sometimes
  selects a different (but related) resource id for a case, or orders operation variants
  differently within a phase.
- The remaining sequence misses are consequently almost all "one field off within an otherwise
  correct array".

These are recorded as the next optimisation target rather than hidden; status accuracy — the
highest-weighted signal — is already saturated on the sample set.
