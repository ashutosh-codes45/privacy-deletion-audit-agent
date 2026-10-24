"""Latency, token, and financial telemetry for a dataset sweep.

What this module measures
-------------------------
For every item in a sweep (one deletion case = one item) it records:

* the wall-clock start and end of the ``audit block``, from which it derives the
  average execution latency per item together with distribution statistics;
* the model name used for each model call, the input and output tokens consumed,
  and the call count;
* the real-time cost of each call, derived from the pricing constraints declared
  in :mod:`config` (US dollars per million tokens), with an environment override.

When the sweep completes, :meth:`MetricsTracker.finish` writes an aggregated,
human-readable summary to ``code/evaluation/usage_report.md`` covering the items
required by AGENTS.md section 6.8: providers and model names, model-call counts,
input/output tokens, total and average tokens per request, and estimated total
and per-request cost, per model and overall.

Typical use
-----------
::

    from evaluation.metrics_tracker import MetricsTracker

    tracker = MetricsTracker(sweep_name="full_dataset_run",
                             expected_items=518)
    for request in requests:
        with tracker.audit_block(request["case_id"]) as block:
            ...                      # decide the case
            block.add_usage(model="DeepSeek-V4.1-Flash",
                            input_tokens=1200, output_tokens=350)
    tracker.finish()                 # -> code/evaluation/usage_report.md

The tracker is dependency-free (standard library only), deterministic in its
output ordering, and safe to use from multiple worker threads: aggregates are
guarded by a lock, while the "currently open block" is thread-local.
"""

from __future__ import annotations

import functools
import math
import statistics
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

try:  # imported as part of the ``code`` package
    import config
except ImportError:  # pragma: no cover - direct execution / flat imports
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import config


TOKENS_PER_MTOK: float = 1_000_000.0
_UNRESOLVED_MODEL: str = "unresolved"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_int(value: int) -> str:
    return f"{value:,}"


def _fmt_money(value: float, places: int = 4) -> str:
    return f"${value:,.{places}f}"


def _fmt_seconds(value: float) -> str:
    return f"{value:,.3f} s"


def _fmt_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _percentile(sorted_values: Sequence[float], quantile: float) -> float:
    """Nearest-rank percentile; deterministic and defined for one-element input."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = max(1, math.ceil(quantile * len(sorted_values)))
    return float(sorted_values[min(rank, len(sorted_values)) - 1])


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelPricing:
    """Pricing constraints for one model, expressed per million tokens."""

    provider: str
    input_per_mtok: float
    output_per_mtok: float
    verified: bool = False

    @classmethod
    def from_mapping(cls, model: str, mapping: Dict[str, Any]) -> "ModelPricing":
        return cls(
            provider=str(mapping.get("provider", "unknown")),
            input_per_mtok=float(mapping.get("input_per_mtok", 0.0) or 0.0),
            output_per_mtok=float(mapping.get("output_per_mtok", 0.0) or 0.0),
            verified=bool(mapping.get("verified", False)),
        )

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        """Real-time cost of one call in :data:`config.PRICING_CURRENCY`."""
        return (
            input_tokens / TOKENS_PER_MTOK * self.input_per_mtok
            + output_tokens / TOKENS_PER_MTOK * self.output_per_mtok
        )


# ---------------------------------------------------------------------------
# Usage accumulators
# ---------------------------------------------------------------------------


@dataclass
class ModelUsage:
    """Accumulated token and cost usage for a single model."""

    model: str
    provider: str = "unknown"
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add_call(self, input_tokens: int, output_tokens: int, cost: float) -> None:
        self.calls += 1
        self.input_tokens += max(0, int(input_tokens))
        self.output_tokens += max(0, int(output_tokens))
        self.cost += float(cost)

    def merge(self, other: "ModelUsage") -> None:
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost += other.cost


@dataclass(frozen=True)
class LatencyStats:
    """Latency distribution for the audited items, in seconds per item."""

    count: int
    total_seconds: float
    mean_seconds: float
    min_seconds: float
    max_seconds: float
    p50_seconds: float
    p90_seconds: float
    p95_seconds: float

    @classmethod
    def from_samples(cls, samples: Sequence[float]) -> "LatencyStats":
        if not samples:
            return cls(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        ordered = sorted(float(s) for s in samples)
        return cls(
            count=len(ordered),
            total_seconds=float(sum(ordered)),
            mean_seconds=float(statistics.fmean(ordered)),
            min_seconds=ordered[0],
            max_seconds=ordered[-1],
            p50_seconds=_percentile(ordered, 0.50),
            p90_seconds=_percentile(ordered, 0.90),
            p95_seconds=_percentile(ordered, 0.95),
        )


@dataclass
class ItemRecord:
    """Result of one audited item."""

    item_id: str
    latency_seconds: float
    started_at: str
    ended_at: str
    ok: bool = True
    error: Optional[str] = None
    usage: Dict[str, ModelUsage] = field(default_factory=dict)


@dataclass
class RunSummary:
    """Fully aggregated view of one sweep."""

    sweep_name: str
    run_id: str
    started_at: str
    finished_at: str
    wall_seconds: float
    items_total: int
    items_ok: int
    items_failed: int
    expected_items: Optional[int]
    latency: LatencyStats
    per_model: Dict[str, ModelUsage] = field(default_factory=dict)
    total_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost: float = 0.0
    pricing_notes: List[str] = field(default_factory=list)
    failed_item_ids: List[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def avg_tokens_per_item(self) -> float:
        return self.total_tokens / self.items_total if self.items_total else 0.0

    @property
    def avg_cost_per_item(self) -> float:
        return self.total_cost / self.items_total if self.items_total else 0.0

    @property
    def avg_calls_per_item(self) -> float:
        return self.total_calls / self.items_total if self.items_total else 0.0

    @property
    def is_complete_sweep(self) -> bool:
        if self.expected_items is None:
            return self.items_failed == 0
        return self.items_total == self.expected_items and self.items_failed == 0


# ---------------------------------------------------------------------------
# Thread-local "currently open block"
# ---------------------------------------------------------------------------


class _BlockStack(threading.local):
    def __init__(self) -> None:
        self.stack: List["AuditBlock"] = []


# ---------------------------------------------------------------------------
# Audit block handle
# ---------------------------------------------------------------------------


class AuditBlock:
    """Handle for one timed audit block; records latency and model usage."""

    __slots__ = ("item_id", "_tracker", "_started_perf", "_started_at", "_usage", "error", "_closed")

    def __init__(self, item_id: str, tracker: "MetricsTracker") -> None:
        self.item_id = item_id
        self._tracker = tracker
        self._started_perf = time.perf_counter()
        self._started_at = _utc_now_iso()
        self._usage: Dict[str, ModelUsage] = {}
        self.error: Optional[str] = None
        self._closed = False

    # -- usage capture -----------------------------------------------------

    def add_usage(self, model: str, input_tokens: int, output_tokens: int) -> ModelUsage:
        """Account for one model call made inside this block."""
        cost = self._tracker.cost_for(model, input_tokens, output_tokens)
        entry = self._usage.get(model)
        if entry is None:
            entry = ModelUsage(model=model, provider=self._tracker.provider_for(model))
            self._usage[model] = entry
        entry.add_call(input_tokens, output_tokens, cost)
        return entry

    def mark_failed(self, error: Any) -> None:
        """Flag the item as failed without aborting the sweep."""
        self.error = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)

    # -- timing ------------------------------------------------------------

    @property
    def elapsed_seconds(self) -> float:
        return time.perf_counter() - self._started_perf

    def close(self) -> ItemRecord:
        """Close the block and hand the record to the tracker (once)."""
        if self._closed:
            raise RuntimeError(f"audit block for {self.item_id!r} already closed")
        self._closed = True
        record = ItemRecord(
            item_id=self.item_id,
            latency_seconds=time.perf_counter() - self._started_perf,
            started_at=self._started_at,
            ended_at=_utc_now_iso(),
            ok=self.error is None,
            error=self.error,
            usage=self._usage,
        )
        self._tracker._commit(record)
        return record


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class MetricsTracker:
    """Collects latency, token, and cost telemetry across a whole sweep."""

    def __init__(
        self,
        sweep_name: str = "full_dataset_run",
        expected_items: Optional[int] = config.EXPECTED_CASE_COUNT,
        report_path: Path = config.USAGE_REPORT_MD,
        pricing_table: Optional[Dict[str, Dict[str, Any]]] = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.sweep_name = sweep_name
        self.expected_items = expected_items
        self.report_path = Path(report_path)
        self.run_id = uuid.uuid4().hex[:12]
        self._pricing = pricing_table if pricing_table is not None else config.load_pricing_table()
        self._clock = clock

        self._lock = threading.RLock()
        self._local = _BlockStack()
        self._records: List[ItemRecord] = []
        self._started_perf = clock()
        self._started_at = _utc_now_iso()
        self._finished = False
        self._pricing_notes: List[str] = []

    # -- pricing -----------------------------------------------------------

    def pricing_for(self, model: str) -> ModelPricing:
        """Pricing entry for ``model``; unknown models fall back to ``DEFAULT``."""
        raw = self._pricing.get(model)
        fallback = False
        if raw is None:
            fallback = True
            raw = self._pricing.get("DEFAULT", {})
        pricing = ModelPricing.from_mapping(model, dict(raw))
        if fallback:
            self._note(
                f"model {model!r} has no pricing entry; charged at DEFAULT rates "
                f"({pricing.input_per_mtok}/{pricing.output_per_mtok} per Mtok)"
            )
        elif not pricing.verified:
            self._note(
                f"model {model!r} uses an unverified placeholder rate "
                f"({pricing.input_per_mtok}/{pricing.output_per_mtok} per Mtok); "
                f"replace it in config.PRICING_TABLE or via {config.PRICING_ENV_VAR}"
            )
        return pricing

    def provider_for(self, model: str) -> str:
        return self.pricing_for(model).provider

    def cost_for(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Real-time cost of one call under the configured pricing constraints."""
        return self.pricing_for(model).cost(input_tokens, output_tokens)

    def _note(self, message: str) -> None:
        with self._lock:
            if message not in self._pricing_notes:
                self._pricing_notes.append(message)

    # -- item tracking -----------------------------------------------------

    @contextmanager
    def audit_block(self, item_id: str) -> Iterator[AuditBlock]:
        """Time one audited item; records latency even if the block raises."""
        block = AuditBlock(item_id, self)
        self._local.stack.append(block)
        try:
            yield block
        except BaseException as exc:  # keep the sweep alive, record the failure
            block.mark_failed(exc)
            raise
        finally:
            if self._local.stack and self._local.stack[-1] is block:
                self._local.stack.pop()
            block.close()

    def tracked(
        self,
        item_id_fn: Optional[Callable[[Any], str]] = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator form of :meth:`audit_block`.

        The wrapped function is called with the item as its first argument; the
        item id defaults to ``item["case_id"]`` and can be overridden with
        ``item_id_fn``. Model calls made inside the call are captured by
        :meth:`record_model_call`.
        """

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            @functools.wraps(func)
            def wrapper(item: Any, *args: Any, **kwargs: Any) -> Any:
                item_id = item_id_fn(item) if item_id_fn else _default_item_id(item)
                with self.audit_block(item_id):
                    return func(item, *args, **kwargs)

            return wrapper

        return decorator

    def record_model_call(self, model: str, input_tokens: int, output_tokens: int) -> None:
        """Attribute a model call to the innermost open block on this thread.

        When no block is open the call is booked against ``item_id="unscoped"``
        so token totals never silently disappear from the report.
        """
        for block in reversed(self._local.stack):
            block.add_usage(model, input_tokens, output_tokens)
            return
        with self.audit_block(_UNRESOLVED_MODEL) as block:
            block.add_usage(model, input_tokens, output_tokens)

    def _commit(self, record: ItemRecord) -> None:
        with self._lock:
            self._records.append(record)

    # -- aggregation -------------------------------------------------------

    def finalize(self) -> RunSummary:
        """Aggregate all recorded items into a :class:`RunSummary`."""
        with self._lock:
            self._finished = True
            records = list(self._records)
            notes = list(self._pricing_notes)

        per_model: Dict[str, ModelUsage] = {}
        for record in records:
            for model, usage in record.usage.items():
                bucket = per_model.get(model)
                if bucket is None:
                    bucket = ModelUsage(model=model, provider=usage.provider)
                    per_model[model] = bucket
                bucket.merge(usage)

        failed = [r.item_id for r in records if not r.ok]
        summary = RunSummary(
            sweep_name=self.sweep_name,
            run_id=self.run_id,
            started_at=self._started_at,
            finished_at=_utc_now_iso(),
            wall_seconds=self._clock() - self._started_perf,
            items_total=len(records),
            items_ok=len(records) - len(failed),
            items_failed=len(failed),
            expected_items=self.expected_items,
            latency=LatencyStats.from_samples([r.latency_seconds for r in records]),
            per_model=dict(sorted(per_model.items())),
            pricing_notes=notes,
            failed_item_ids=failed,
        )
        summary.total_calls = sum(u.calls for u in summary.per_model.values())
        summary.total_input_tokens = sum(u.input_tokens for u in summary.per_model.values())
        summary.total_output_tokens = sum(u.output_tokens for u in summary.per_model.values())
        summary.total_cost = sum(u.cost for u in summary.per_model.values())
        return summary

    # -- reporting ---------------------------------------------------------

    def render_markdown(self, summary: Optional[RunSummary] = None) -> str:
        """Render the aggregated summary in a clean, human-readable layout."""
        summary = summary or self.finalize()
        currency = config.PRICING_CURRENCY
        status = "COMPLETE" if summary.is_complete_sweep else "PARTIAL"
        expected = (
            _fmt_int(summary.expected_items) if summary.expected_items is not None else "n/a"
        )

        lines: List[str] = []
        add = lines.append
        add(f"# Usage Report — {summary.sweep_name}")
        add("")
        add(f"- Run id: `{summary.run_id}`")
        add(f"- Status: **{status}**")
        add(f"- Started (UTC): {summary.started_at}")
        add(f"- Finished (UTC): {summary.finished_at}")
        add(f"- Wall-clock duration: {_fmt_duration(summary.wall_seconds)}")
        add(f"- Items audited: {_fmt_int(summary.items_total)} of {expected} expected")
        add(f"- Items succeeded: {_fmt_int(summary.items_ok)}")
        add(f"- Items failed: {_fmt_int(summary.items_failed)}")
        add(f"- Model calls: {_fmt_int(summary.total_calls)}")
        add(f"- Currency: {currency}")
        add("")

        add("## Latency per audited item")
        add("")
        add("| Metric | Value |")
        add("| --- | --- |")
        add(f"| Items measured | {_fmt_int(summary.latency.count)} |")
        add(f"| Total audit time | {_fmt_seconds(summary.latency.total_seconds)} |")
        add(f"| Average latency per item | {_fmt_seconds(summary.latency.mean_seconds)} |")
        add(f"| Median (p50) | {_fmt_seconds(summary.latency.p50_seconds)} |")
        add(f"| p90 | {_fmt_seconds(summary.latency.p90_seconds)} |")
        add(f"| p95 | {_fmt_seconds(summary.latency.p95_seconds)} |")
        add(f"| Fastest item | {_fmt_seconds(summary.latency.min_seconds)} |")
        add(f"| Slowest item | {_fmt_seconds(summary.latency.max_seconds)} |")
        throughput = (
            1.0 / summary.latency.mean_seconds if summary.latency.mean_seconds > 0 else 0.0
        )
        add(f"| Throughput (mean) | {throughput:,.2f} items/s |")
        add("")

        add("## Token and cost usage by model")
        add("")
        if summary.per_model:
            add("| Provider | Model | Calls | Input tokens | Output tokens | Total tokens | Avg tokens/request | Cost (USD) | Avg cost/request |")
            add("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
            for usage in summary.per_model.values():
                avg_tokens = usage.total_tokens / summary.items_total if summary.items_total else 0.0
                avg_cost = usage.cost / summary.items_total if summary.items_total else 0.0
                add(
                    f"| {usage.provider} | {usage.model} | {_fmt_int(usage.calls)} "
                    f"| {_fmt_int(usage.input_tokens)} | {_fmt_int(usage.output_tokens)} "
                    f"| {_fmt_int(usage.total_tokens)} | {avg_tokens:,.1f} "
                    f"| {_fmt_money(usage.cost)} | {_fmt_money(avg_cost, 6)} |"
                )
        else:
            add("_No model calls were recorded during this sweep._")
        add("")

        add("## Overall totals")
        add("")
        add("| Metric | Value |")
        add("| --- | --- |")
        add(f"| Model calls | {_fmt_int(summary.total_calls)} |")
        add(f"| Model calls per request | {summary.avg_calls_per_item:,.2f} |")
        add(f"| Input tokens | {_fmt_int(summary.total_input_tokens)} |")
        add(f"| Output tokens | {_fmt_int(summary.total_output_tokens)} |")
        add(f"| Total tokens | {_fmt_int(summary.total_tokens)} |")
        add(f"| Average tokens per request | {summary.avg_tokens_per_item:,.1f} |")
        add(f"| Estimated total cost | {_fmt_money(summary.total_cost)} {currency} |")
        add(f"| Estimated cost per request | {_fmt_money(summary.avg_cost_per_item, 6)} {currency} |")
        add("")

        add("## Pricing constraints applied")
        add("")
        add("| Model | Input (USD / 1M tokens) | Output (USD / 1M tokens) | Verified |")
        add("| --- | ---: | ---: | --- |")
        for model in summary.per_model:
            pricing = self.pricing_for(model)
            add(
                f"| {model} | {pricing.input_per_mtok:,.4f} | {pricing.output_per_mtok:,.4f} "
                f"| {'yes' if pricing.verified else 'no (placeholder)'} |"
            )
        add("")
        if summary.pricing_notes:
            add("Notes:")
            add("")
            for note in summary.pricing_notes:
                add(f"- {note}")
        else:
            add("_All models were charged at verified rates._")
        add("")

        if summary.failed_item_ids:
            shown = ", ".join(summary.failed_item_ids[:20])
            suffix = "" if len(summary.failed_item_ids) <= 20 else f" … (+{len(summary.failed_item_ids) - 20} more)"
            add("## Failed items")
            add("")
            add(f"{_fmt_int(summary.items_failed)} item(s) did not complete: {shown}{suffix}")
            add("")

        add("## Reproduce")
        add("")
        add("```text")
        add("python code/main.py            # audits dataset/deletion_requests.csv -> output.csv")
        add("python -m evaluation.metrics_tracker --demo   # render this layout without a run")
        add("```")
        add("")
        return "\n".join(lines)

    def write_report(
        self,
        path: Optional[Path] = None,
        summary: Optional[RunSummary] = None,
    ) -> Path:
        """Write the aggregated summary to ``usage_report.md`` (default path)."""
        target = Path(path) if path is not None else self.report_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.render_markdown(summary), encoding=config.CSV_ENCODING, newline=config.CSV_NEWLINE)
        return target

    def finish(
        self,
        expected_items: Optional[int] = None,
        report_path: Optional[Path] = None,
    ) -> RunSummary:
        """Finalize the sweep, write the report, and return the summary."""
        if expected_items is not None:
            self.expected_items = expected_items
        summary = self.finalize()
        self.write_report(report_path, summary)
        return summary

    # -- introspection -----------------------------------------------------

    @property
    def records(self) -> List[ItemRecord]:
        with self._lock:
            return list(self._records)

    @property
    def items_recorded(self) -> int:
        with self._lock:
            return len(self._records)


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------


@contextmanager
def sweep(
    sweep_name: str = "full_dataset_run",
    expected_items: Optional[int] = config.EXPECTED_CASE_COUNT,
    report_path: Path = config.USAGE_REPORT_MD,
) -> Iterator[MetricsTracker]:
    """Run a sweep and always emit ``usage_report.md`` when it ends."""
    tracker = MetricsTracker(
        sweep_name=sweep_name,
        expected_items=expected_items,
        report_path=report_path,
    )
    try:
        yield tracker
    finally:
        tracker.finish()


def _default_item_id(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("case_id", "case", "id"):
            if key in item:
                return str(item[key])
    return str(getattr(item, "case_id", item))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _render_demo() -> str:
    """Render a synthetic 8-item sweep so the report layout can be reviewed."""
    tracker = MetricsTracker(sweep_name="demo_sweep", expected_items=8)
    lats = [0.42, 0.51, 0.38, 1.24, 0.47, 0.55, 2.10, 0.49]
    calls = [(980, 210), (1140, 260), (890, 180), (1320, 340), (1010, 230), (1080, 250), (1450, 380), (960, 205)]
    for index, (latency, (in_tok, out_tok)) in enumerate(zip(lats, calls), start=1):
        with tracker.audit_block(f"case_{index:02d}") as block:
            time.sleep(min(latency / 200.0, 0.01))
            tracker.record_model_call(config.DEFAULT_MODEL, in_tok, out_tok)
            if index == 8:
                block.mark_failed(ValueError("simulated upstream timeout"))
    return tracker.render_markdown()


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--demo", action="store_true", help="render a synthetic report to stdout")
    parser.add_argument("--out", type=Path, default=None, help="write the report to this path instead")
    args = parser.parse_args(argv)

    report = _render_demo()
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding=config.CSV_ENCODING, newline=config.CSV_NEWLINE)
        print(f"wrote {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
