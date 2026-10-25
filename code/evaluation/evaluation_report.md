# Evaluation Report — Ghost Data Still Exists

Deletion-audit agent for HackerRank Orchestrate (October 2026).

* **Solution entry point:** `code/main.py` (deterministic engine in `code/audit_engine.py`)
* **Scorer:** `code/evaluation/local_scorer.py` (read-only; audits the 32 public samples)
* **Sample set scored:** `dataset/sample_deletion_requests.csv` against `dataset/sample_output.csv`

---

## 1. Case-Level Deletion-Status Accuracy

* **Exact-Match Accuracy:** **100.00%** (32/32 cases matched perfectly on the development samples)
* **Micro Precision / Recall / F1:** P 100.00% / R 100.00% / F1 100.00%
* **Macro Precision / Recall / F1:** P 100.00% / R 100.00% / F1 100.00%

| Class | Support | TP | FP | FN | Precision | Recall | F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| COMPLETE | 10 | 10 | 0 | 0 | 100.00% | 100.00% | 100.00% |
| INCOMPLETE | 22 | 22 | 0 | 0 | 100.00% | 100.00% | 100.00% |
| NOT_APPLICABLE | 0 | 0 | 0 | 0 | 100.00% | 100.00% | 100.00% |

*Note: The 32 public development samples contain zero `NOT_APPLICABLE` items. This path is validated separately via custom unit case C5 below.*

## 2. Step-Field Accuracy (Step-Level Metrics)

* **Field Micro Precision / Recall / F1:** P 85.04% / R 84.60% / **F1 84.82%**
* **Field Macro Precision / Recall / F1:** P 85.04% / R 84.60% / F1 84.82%
* **Steps (Predicted vs. Expected):** 192 vs. 193

| Step field | Support | TP | FP | FN | Precision | Recall | F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `step_no` | 193 | 183 | 9 | 10 | 95.31% | 94.82% | 95.06% |
| `phase` | 193 | 174 | 18 | 19 | 90.62% | 90.16% | 90.39% |
| `system_id` | 193 | 161 | 31 | 32 | 83.85% | 83.42% | 83.64% |
| `resource_id` | 193 | 147 | 45 | 46 | 76.56% | 76.17% | 76.36% |
| `operation` | 193 | 150 | 42 | 43 | 78.12% | 77.72% | 77.92% |
| `state` | 193 | 156 | 36 | 37 | 81.25% | 80.83% | 81.04% |
| `evidence_id` | 193 | 172 | 20 | 21 | 89.58% | 89.12% | 89.35% |

## 3. Sequence Accuracy (Exact Whole-Array Match)

* **Exact Step-Sequence Matches:** **31.25%** (10/32 cases matched perfectly element-for-element)

## 4. Custom Evaluation Test Cases

### C1 — Split Trace Record Validation
* **Input:** Split lines inside `evidence_94` sharing a distinct `trace_id`.
* **Expected:** The engine checks both lines, lets the terminal status (`rows=0 http_status=404`) win the context tie-breaker over the opening line, and flags the record `ABSENT`.
* **Improvement:** Prevents initial trace flags from creating false `INCOMPLETE` statuses.

### C2 — Recursive JSON Array Crawling
* **Input:** Deeply nested lists inside `evidence_92` under `service_exports.account_service`.
* **Expected:** The child profile (`account_6440` belonging to `user_21`) must be indexed correctly by the walker even if the parent node contains multiple independent resource IDs.
* **Improvement:** Eliminates dropped context records across deep data export bundles.

### C3 — Strict Database Tombstone Inspection
* **Input:** Relational snapshot row inside `evidence_51` where `name`, `email`, and `phone` values are all completely null/blank.
* **Expected:** Flags the resource state cleanly as `REDACTED` or `ANONYMIZED` matching compliance constraints while preserving the stable user token.
* **Improvement:** Prevents cleared rows from registering as remaining PII leaks.

### C4 — Dependency Data Flows
* **Input:** System records containing active connector structures (`data_flow_6244`).
* **Expected:** Maps the resource category to `DISABLE_DATA_FLOW` actions before modifying data targets.
* **Improvement:** Safely untangles looping synchronization pipelines.

### C5 — Multilingual Triage Routing
* **Input:** Inbound request text strings containing opt-out or details query commands in French/German.
* **Expected:** Routes outcomes to `NOT_APPLICABLE` with `[]` empty steps.
* **Improvement:** Isolates administrative questions from authorized data wipe executions.

## 5. Failures Found and Improvements Made

| # | Failure Observed | Root Cause | Fix (Module) | Engineering Effect |
| --- | --- | --- | --- | --- |
| 1 | Relational database drift missed | Evaluated primary tables only | Scanned both primary and failover replica blocks (`audit_engine.py`) | Discovered unsynced ghost records surviving on failover shards |
| 2 | Nested JSON objects dropped | Flat iterative dictionary loop skipped arrays | Implemented recursive value-tree walker (`audit_engine.py`) | Stopped dropped context records across deep data export bundles |
| 3 | Split event traces lost subject | Closing statuses lacked direct identifier columns | Linked disjointed log events by tracking unique `trace_id` sets | Correctly associated `support_002` and `search_898` elements |
| 4 | Initial trace noise triggered false leaks | Tie-breaker selected latest timestamp alone | Implemented `_context_weight` parameter logic | Terminal outcomes (`rows=0`) override initial processing alerts |
| 5 | Cleared tombstones flagged as leaks | `str(None)` execution translated values to string `"None"` | Deployed `_direct_identifiers_cleared()` validation checks | Verified successful data redactions across storage tables |
