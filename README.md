# Privacy Deletion Audit Agent — Ghost Data Still Exists

An automated, high-throughput compliance agent built for the HackerRank Orchestrate (October 2026) challenge. This system audits corporate storage infrastructure, system log networks, and replica data clusters to verify if data deletion requests were completely executed or if "Ghost Data" footprints remain accessible.

## 1. Architectural Architecture & Strategy

The engine utilizes a strict **two-tier deterministic pipeline strategy** designed to run with maximum execution throughput and zero token reasoning overhead:

1. **Deterministic Processing Core (First Tier):** The agent indexes relational user databases and parses structured event tracking tables locally. It tracks historical identity mutations across a multi-hop lineage graph up to a case's absolute `audit_at` execution cutoff.
2. **Context-Weight Log Lineage Analysis (Second Tier):** To parse raw system logs (`.log`) and structured dumps (`.json`) without expensive LLM latency, the system maps asynchronous processing sequences using shared `trace_id` blocks. A custom context-weight ranking mechanism ensures terminal query logs (e.g., `rows=0 http_status=404`) systematically take precedence over initial trace footprints.
3. **Absence Verification Guards:** The pipeline enforces a rule requiring all cases marked `COMPLETE` to demonstrate concrete, verifiable data absence across primary nodes and failover replicas before closing.

## 2. Setup & Local Execution Instructions

### Prerequisites
* Python 3.10 or higher
* Standard python package installer (`pip`)

### Installation
Clone the repository and install the baseline requirements:
```bash
pip install -r requirements.txt
```

### Running the Production Pipeline
To process the 518 production records in the dataset and generate the submission matrix, execute the main entry point from the repository root:
```bash
python code/main.py
```
This command processes all requests and writes the clean compliance arrays directly to `output.csv` at the repository root.

### Running the Local Scorer
To execute row-by-row validation testing against the 32 sample ground-truth answers:
```bash
python code/evaluation/local_scorer.py
```

## 3. Known System Limitations

* **Multimodal Assets (.png):** Account profile screenshots and binary visual verification attachments are currently skipped during this high-speed deterministic processing pass. Full multimodal parsing requires activating an external Vision-Language Model (VLM) pipeline.
* **Unstructured PDFs:** Complex document layouts or multi-column text strings within support attachments are limited to linear string filtering loops, which can skip highly complex nested block entities.
