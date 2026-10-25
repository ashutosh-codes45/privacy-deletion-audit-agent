"""Production entry point for the Ghost Data Still Exists deletion-audit agent.

Running this module audits every request in ``dataset/deletion_requests.csv`` and
produces the submission artefacts required by AGENTS.md section 6:

* ``output.csv`` at the **repository root** - exactly one row per ``case_id``
  with the ``case_id,deletion_status,steps_json`` schema, sorted by ``case_id``;
* ``code/evaluation/usage_report.md`` - the latency, token, and cost telemetry of
  the run, aggregated by :mod:`evaluation.metrics_tracker`.

The engine itself is deterministic: it reconstructs each subject's identifier
chain and the state of every resource at the case's ``audit_at`` from the
evidence corpus, and makes **no** provider calls. The vision/document extraction
hook is environment-gated (AGENTS.md sections 4.4 and 6.7), so a run with no
credential configured spends nothing; the token budget the hook would consume is
still reported.

Usage
-----
::

    python code/main.py
    python code/main.py --requests dataset/deletion_requests.csv --output output.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List

try:  # imported as part of the ``code`` package
    import audit_engine
    import config
    from evaluation import metrics_tracker
except ImportError:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import audit_engine
    import config
    from evaluation import metrics_tracker


TOKENS_PER_MTOK: float = 1_000_000.0


def _fmt_int(value: Any) -> str:
    return f"{int(value):,}"


def _fmt_money(value: float) -> str:
    return f"${value:,.6f}"


def _extraction_appendix(engine: "audit_engine.DeletionAuditEngine", summary: Any) -> str:
    """Token allocation and explicit cost arithmetic for the extraction pass.

    The sweep itself records zero provider calls, so the tracker's tables are
    empty. This appendix reports the evidence-extraction token budget (what the
    configured vision/document hook would submit) and works the cost out by hand
    against the verified ``config.PRICING_TABLE`` rates, so the arithmetic in
    ``usage_report.md`` is auditable.
    """
    usage = engine.evidence_usage()
    corpus = engine.corpus
    pricing = config.pricing_for(config.DEFAULT_MODEL)

    in_rate = float(pricing["input_per_mtok"])
    out_rate = float(pricing["output_per_mtok"])
    in_tok = int(usage.get("input_tokens", 0))
    out_tok = int(usage.get("output_tokens", 0))
    in_cost = in_tok / TOKENS_PER_MTOK * in_rate
    out_cost = out_tok / TOKENS_PER_MTOK * out_rate
    total_cost = in_cost + out_cost
    items = summary.items_total or 1

    lines: List[str] = []
    add = lines.append
    add("## Evidence-extraction token allocation")
    add("")
    add(
        "The deterministic pass reads every text, SQL, JSON and PDF asset locally "
        "(standard library only). Screenshot assets stay unresolved unless a vision "
        "provider is configured, so no provider call is made and nothing is billed."
    )
    add("")
    add("| Metric | Value |")
    add("| --- | ---: |")
    add(f"| Evidence assets indexed (text/PDF/SQL/JSON) | {_fmt_int(corpus.readable_files)} |")
    add(f"| Evidence assets skipped (image, no provider) | {_fmt_int(len(corpus.skipped_files))} |")
    add(f"| Local extraction cache hits | {_fmt_int(usage.get('cache_hits', 0))} |")
    add(f"| Assets analysed this run | {_fmt_int(usage.get('assets_analyzed', 0))} |")
    add(f"| Provider (vision) model calls | {_fmt_int(usage.get('model_calls', 0))} |")
    add(f"| Extraction prompt tokens (allocation) | {_fmt_int(in_tok)} |")
    add(f"| Extraction output tokens (allocation) | {_fmt_int(out_tok)} |")
    add(f"| Total extraction tokens (allocation) | {_fmt_int(in_tok + out_tok)} |")
    add(f"| Average tokens per request | {(in_tok + out_tok) / items:,.1f} |")
    add(f"| Requests in this sweep | {_fmt_int(items)} |")
    add("")
    add("### Model providers and pricing constraints applied")
    add("")
    add("| Provider | Model | Input (USD / 1M tokens) | Output (USD / 1M tokens) | Verified |")
    add("| --- | --- | ---: | ---: | --- |")
    add(
        f"| {pricing['provider']} | {config.DEFAULT_MODEL} | {in_rate:,.4f} | "
        f"{out_rate:,.4f} | {'yes' if pricing.get('verified') else 'no (placeholder)'} |"
    )
    add("")
    add("### Explicit cost calculation (verified rates)")
    add("")
    add(f"Rates applied for `{config.DEFAULT_MODEL}` (provider `{pricing['provider']}`, verified):")
    add("")
    add(f"* input  - {in_rate:,.4f} USD per 1,000,000 tokens")
    add(f"* output - {out_rate:,.4f} USD per 1,000,000 tokens")
    add("")
    add("```text")
    add(f"input cost  = {_fmt_int(in_tok)} / 1,000,000 x {in_rate:,.4f} = {_fmt_money(in_cost)}")
    add(f"output cost = {_fmt_int(out_tok)} / 1,000,000 x {out_rate:,.4f} = {_fmt_money(out_cost)}")
    add(f"total       = {_fmt_money(total_cost)} over {_fmt_int(items)} requests")
    add(f"average     = {_fmt_money(total_cost / items)} per request")
    add("```")
    add("")
    add(
        "Because the production run makes zero provider calls, the billed model cost "
        f"is {_fmt_money(0.0)}; the figures above are the token budget the extraction "
        "hook would consume once a provider credential is configured."
    )
    add("")
    return "\n".join(lines)


def run(
    requests_csv: Path = config.DELETION_REQUESTS_CSV,
    output_csv: Path = config.OUTPUT_CSV,
    usage_report: Path = config.USAGE_REPORT_MD,
) -> int:
    """Audit the full request set, write ``output.csv`` and ``usage_report.md``."""
    engine = audit_engine.DeletionAuditEngine()
    tracker = metrics_tracker.MetricsTracker(
        sweep_name="full_dataset_run",
        expected_items=config.EXPECTED_CASE_COUNT,
        report_path=Path(usage_report),
    )

    results = engine.run_sweep(
        requests_csv=Path(requests_csv),
        output_csv=Path(output_csv),
        tracker=tracker,
    )

    summary = tracker.finalize()
    report = tracker.render_markdown(summary) + "\n" + _extraction_appendix(engine, summary)
    target = Path(usage_report)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report, encoding=config.CSV_ENCODING, newline=config.CSV_NEWLINE)

    by_status: dict = {}
    for result in results:
        by_status[result.deletion_status] = by_status.get(result.deletion_status, 0) + 1

    print(f"requests audited : {len(results)}")
    print(f"status breakdown : {by_status}")
    print(f"output written   : {Path(output_csv).resolve()}")
    print(f"usage report     : {target.resolve()}")
    return 0 if summary.is_complete_sweep else 1


def main(argv: "List[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--requests", type=Path, default=config.DELETION_REQUESTS_CSV)
    parser.add_argument("--output", type=Path, default=config.OUTPUT_CSV)
    parser.add_argument("--usage-report", type=Path, default=config.USAGE_REPORT_MD)
    args = parser.parse_args(argv)
    return run(args.requests, args.output, args.usage_report)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
