"""Local validation scorer for the deterministic baseline audit engine.

Purpose
-------
Run :class:`audit_engine.DeletionAuditEngine` over the *public* sample set
(``dataset/sample_deletion_requests.csv``), compare the result row by row against
the published answers (``dataset/sample_output.csv``), and report explicit
precision / recall / F1 for two things:

* ``deletion_status`` — treated as a three-class label
  (``COMPLETE`` / ``INCOMPLETE`` / ``NOT_APPLICABLE``);
* step *values* — every field of every step, aligned by ``(case_id, step_no)``,
  so a step in the wrong position counts as both a false positive and a false
  negative just as the sequence-sensitive grader would treat it.

Why ``(case_id, step_no)`` alignment
------------------------------------
AGENTS.md section 6.3 says JSON object-key order and whitespace are ignored but
array order is preserved. Comparing steps positionally is therefore the strictest
useful measure: a correct step placed at the wrong index is *not* a match.

Usage
-----
::

    python code/evaluation/local_scorer.py                 # score the sample set
    python code/evaluation/local_scorer.py --out report.md # also write markdown
    python code/evaluation/local_scorer.py --show-mismatches

The scorer is read-only with respect to ``output.csv``: it never runs the sweep,
so it cannot overwrite the submission artefact.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:  # imported as part of the ``code`` package
    import config
    import audit_engine as engine
except ImportError:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import config
    import audit_engine as engine


STATUS_CLASSES: Tuple[str, ...] = config.DELETION_STATUSES
STEP_FIELDS: Tuple[str, ...] = config.STEP_FIELDS


# ---------------------------------------------------------------------------
# Metric primitives
# ---------------------------------------------------------------------------


def _ratio(numerator: int, denominator: int) -> float:
    """Safe ratio; a 0/0 case (no predictions and no expectations) counts as 1.0."""
    if denominator <= 0:
        return 1.0 if numerator <= 0 else 0.0
    return numerator / denominator


def _f1(precision: float, recall: float) -> float:
    if precision + recall <= 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


@dataclass
class Confusion:
    """TP/FP/FN counters for one label or one step field."""

    label: str
    tp: int = 0
    fp: int = 0
    fn: int = 0
    support: int = 0  # number of gold instances

    @property
    def precision(self) -> float:
        return _ratio(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> float:
        return _ratio(self.tp, self.tp + self.fn)

    @property
    def f1(self) -> float:
        return _f1(self.precision, self.recall)


@dataclass
class CaseComparison:
    """Row-by-row comparison for a single ``case_id``."""

    case_id: str
    gold_status: str
    predicted_status: str
    gold_steps: List[Dict[str, Any]] = field(default_factory=list)
    predicted_steps: List[Dict[str, Any]] = field(default_factory=list)
    gold_error: str = ""
    predicted_error: str = ""
    notes: Tuple[str, ...] = ()

    @property
    def status_match(self) -> bool:
        return self.gold_status == self.predicted_status

    @property
    def steps_match(self) -> bool:
        return self.gold_steps == self.predicted_steps

    def mismatched_fields(self) -> List[str]:
        """Field names that differ at any aligned step position."""
        fields: List[str] = []
        for index in range(max(len(self.gold_steps), len(self.predicted_steps))):
            gold = self.gold_steps[index] if index < len(self.gold_steps) else None
            pred = self.predicted_steps[index] if index < len(self.predicted_steps) else None
            if gold is None or pred is None:
                fields.append(f"step{index + 1}:missing")
                continue
            for name in STEP_FIELDS:
                if gold.get(name) != pred.get(name):
                    fields.append(f"step{index + 1}.{name}")
        return fields


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_requests(path: Path) -> List[Dict[str, str]]:
    """Read a requests CSV into plain dicts, preserving file order."""
    with open(path, "r", encoding=config.CSV_ENCODING, newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def parse_steps(raw: str) -> Tuple[List[Dict[str, Any]], str]:
    """Parse a ``steps_json`` cell; return ``(steps, error_message)``.

    Field order inside a JSON object is irrelevant (AGENTS.md section 6.3), so
    the parsed dicts are compared directly. A malformed cell yields an empty list
    and a non-empty error string rather than aborting the score run.
    """
    text = (raw or "").strip()
    if not text:
        return [], ""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError) as exc:
        return [], f"invalid JSON: {exc}"
    if not isinstance(parsed, list):
        return [], f"steps_json is {type(parsed).__name__}, expected list"
    steps: List[Dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            return steps, f"step is {type(item).__name__}, expected object"
        steps.append({name: item.get(name) for name in STEP_FIELDS})
    return steps, ""


def load_gold(path: Path) -> Dict[str, Dict[str, Any]]:
    """Read the gold ``sample_output.csv`` keyed by ``case_id``."""
    gold: Dict[str, Dict[str, Any]] = {}
    for row in load_requests(path):
        case_id = (row.get("case_id") or "").strip()
        if not case_id:
            continue
        steps, error = parse_steps(row.get("steps_json", ""))
        gold[case_id] = {
            "deletion_status": (row.get("deletion_status") or "").strip(),
            "steps": steps,
            "error": error,
        }
    return gold


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass
class ScoreReport:
    """Full scored comparison for the sample set."""

    comparisons: List[CaseComparison]
    status_confusions: Dict[str, Confusion]
    field_confusions: Dict[str, Confusion]
    gold_step_total: int
    predicted_step_total: int
    missing_cases: List[str]
    extra_cases: List[str]

    # -- aggregate helpers -------------------------------------------------

    @property
    def cases_total(self) -> int:
        return len(self.comparisons)

    @property
    def status_correct(self) -> int:
        return sum(1 for c in self.comparisons if c.status_match)

    @property
    def status_accuracy(self) -> float:
        return _ratio(self.status_correct, self.cases_total)

    @property
    def sequence_correct(self) -> int:
        return sum(1 for c in self.comparisons if c.steps_match)

    @property
    def sequence_accuracy(self) -> float:
        return _ratio(self.sequence_correct, self.cases_total)

    @property
    def status_micro(self) -> Tuple[float, float, float]:
        tp = sum(c.tp for c in self.status_confusions.values())
        fp = sum(c.fp for c in self.status_confusions.values())
        fn = sum(c.fn for c in self.status_confusions.values())
        precision = _ratio(tp, tp + fp)
        recall = _ratio(tp, tp + fn)
        return precision, recall, _f1(precision, recall)

    @property
    def status_macro(self) -> Tuple[float, float, float]:
        active = [c for c in self.status_confusions.values() if c.support > 0 or c.fp > 0]
        if not active:
            return 1.0, 1.0, 1.0
        precision = sum(c.precision for c in active) / len(active)
        recall = sum(c.recall for c in active) / len(active)
        return precision, recall, _f1(precision, recall)

    @property
    def field_micro(self) -> Tuple[float, float, float]:
        tp = sum(c.tp for c in self.field_confusions.values())
        fp = sum(c.fp for c in self.field_confusions.values())
        fn = sum(c.fn for c in self.field_confusions.values())
        precision = _ratio(tp, tp + fp)
        recall = _ratio(tp, tp + fn)
        return precision, recall, _f1(precision, recall)

    @property
    def field_macro(self) -> Tuple[float, float, float]:
        active = list(self.field_confusions.values())
        if not active:
            return 1.0, 1.0, 1.0
        precision = sum(c.precision for c in active) / len(active)
        recall = sum(c.recall for c in active) / len(active)
        return precision, recall, _f1(precision, recall)


def _score_status(comparisons: Sequence[CaseComparison]) -> Dict[str, Confusion]:
    """Per-class TP/FP/FN for the ``deletion_status`` label."""
    confusions = {label: Confusion(label) for label in STATUS_CLASSES}
    for comparison in comparisons:
        gold = comparison.gold_status
        predicted = comparison.predicted_status
        if gold in confusions:
            confusions[gold].support += 1
        if gold == predicted and gold in confusions:
            confusions[gold].tp += 1
            continue
        if gold in confusions:
            confusions[gold].fn += 1
        if predicted in confusions:
            confusions[predicted].fp += 1
        elif predicted:
            # An out-of-vocabulary status is still a false positive for the run.
            confusions.setdefault(predicted, Confusion(predicted)).fp += 1
    return confusions


def _score_fields(comparisons: Sequence[CaseComparison]) -> Dict[str, Confusion]:
    """Per-field TP/FP/FN for step values, aligned by ``(case_id, step_no)``.

    A predicted step whose position holds no gold step is a false positive for
    every field; the missing gold step is a false negative for every field.
    """
    confusions = {name: Confusion(name) for name in STEP_FIELDS}
    for comparison in comparisons:
        gold_steps = comparison.gold_steps
        predicted_steps = comparison.predicted_steps
        for index in range(max(len(gold_steps), len(predicted_steps))):
            gold = gold_steps[index] if index < len(gold_steps) else None
            predicted = predicted_steps[index] if index < len(predicted_steps) else None
            for name in STEP_FIELDS:
                counter = confusions[name]
                if gold is not None:
                    counter.support += 1
                if gold is not None and predicted is not None and gold.get(name) == predicted.get(name):
                    counter.tp += 1
                elif predicted is not None:
                    counter.fp += 1
                    if gold is not None:
                        counter.fn += 1
                elif gold is not None:
                    counter.fn += 1
    return confusions


def score(
    engine_runner: Any,
    requests: Sequence[Dict[str, str]],
    gold: Dict[str, Dict[str, Any]],
) -> ScoreReport:
    """Audit every request and score it against the gold answers."""
    comparisons: List[CaseComparison] = []
    seen: set = set()

    for request in requests:
        case_id = (request.get("case_id") or "").strip()
        seen.add(case_id)
        predicted = engine_runner.run_case(request)
        predicted_steps, predicted_error = parse_steps(
            engine.serialize_steps(predicted.steps)
        )
        entry = gold.get(case_id, {})
        gold_status = (entry.get("deletion_status") or "").strip()
        gold_steps = list(entry.get("steps") or [])
        notes: Tuple[str, ...] = ()
        if not entry:
            notes = ("no gold row for this case",)
        comparisons.append(
            CaseComparison(
                case_id=case_id,
                gold_status=gold_status,
                predicted_status=predicted.deletion_status,
                gold_steps=gold_steps,
                predicted_steps=predicted_steps,
                gold_error=str(entry.get("error") or ""),
                predicted_error=predicted_error,
                notes=notes,
            )
        )

    missing_cases = sorted(set(gold) - seen, key=_case_sort_key)
    return ScoreReport(
        comparisons=comparisons,
        status_confusions=_score_status(comparisons),
        field_confusions=_score_fields(comparisons),
        gold_step_total=sum(len(c.gold_steps) for c in comparisons),
        predicted_step_total=sum(len(c.predicted_steps) for c in comparisons),
        missing_cases=missing_cases,
        extra_cases=[],
    )


def _case_sort_key(case_id: str) -> int:
    digits = "".join(ch for ch in case_id if ch.isdigit())
    return int(digits) if digits else 1 << 30


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _pct(value: float) -> str:
    return f"{value * 100:,.2f}%"


def _triple(scores: Tuple[float, float, float]) -> str:
    return f"P {_pct(scores[0])} / R {_pct(scores[1])} / F1 {_pct(scores[2])}"


def render_markdown(report: ScoreReport, requests_path: Path, gold_path: Path) -> str:
    """Render the scored comparison as human-readable markdown."""
    lines: List[str] = []
    add = lines.append

    add("# Local Validation Score — Deterministic Baseline")
    add("")
    add(f"- Requests scored: `{requests_path.name}`")
    add(f"- Gold answers: `{gold_path.name}`")
    add(f"- Cases compared: {report.cases_total}")
    add(f"- Steps: {report.predicted_step_total} predicted vs {report.gold_step_total} expected")
    add("")

    add("## Deletion status")
    add("")
    micro = report.status_micro
    macro = report.status_macro
    add("| Metric | Value |")
    add("| --- | ---: |")
    add(f"| Exact-match accuracy | {_pct(report.status_accuracy)} ({report.status_correct}/{report.cases_total}) |")
    add(f"| Micro precision / recall / F1 | {_triple(micro)} |")
    add(f"| Macro precision / recall / F1 | {_triple(macro)} |")
    add("")
    add("| Class | Support | TP | FP | FN | Precision | Recall | F1 |")
    add("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for label in STATUS_CLASSES:
        counter = report.status_confusions.get(label)
        if counter is None:
            continue
        add(
            f"| {label} | {counter.support} | {counter.tp} | {counter.fp} | {counter.fn} "
            f"| {_pct(counter.precision)} | {_pct(counter.recall)} | {_pct(counter.f1)} |"
        )
    add("")

    add("## Step values")
    add("")
    add("| Metric | Value |")
    add("| --- | ---: |")
    add(f"| Exact step-sequence matches | {_pct(report.sequence_accuracy)} ({report.sequence_correct}/{report.cases_total}) |")
    add(f"| Field micro precision / recall / F1 | {_triple(report.field_micro)} |")
    add(f"| Field macro precision / recall / F1 | {_triple(report.field_macro)} |")
    add("")
    add("| Step field | Support | TP | FP | FN | Precision | Recall | F1 |")
    add("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for name in STEP_FIELDS:
        counter = report.field_confusions[name]
        add(
            f"| {name} | {counter.support} | {counter.tp} | {counter.fp} | {counter.fn} "
            f"| {_pct(counter.precision)} | {_pct(counter.recall)} | {_pct(counter.f1)} |"
        )
    add("")

    if report.missing_cases:
        shown = ", ".join(report.missing_cases[:20])
        add(f"## Gold cases with no request: {shown}")
        add("")

    return "\n".join(lines)


def render_mismatches(report: ScoreReport) -> str:
    """Render a per-case mismatch listing for debugging."""
    lines: List[str] = []
    for comparison in report.comparisons:
        if comparison.status_match and comparison.steps_match and not comparison.notes:
            continue
        add = lines.append
        add(f"### {comparison.case_id}")
        add(f"- status: gold={comparison.gold_status or '-'} predicted={comparison.predicted_status or '-'}")
        add(f"- steps: {len(comparison.predicted_steps)} predicted vs {len(comparison.gold_steps)} expected")
        fields = comparison.mismatched_fields()
        if fields:
            add(f"- differing: {', '.join(fields[:12])}" + (" …" if len(fields) > 12 else ""))
        if comparison.predicted_error:
            add(f"- predicted steps error: {comparison.predicted_error}")
        if comparison.gold_error:
            add(f"- gold steps error: {comparison.gold_error}")
        for note in comparison.notes:
            add(f"- note: {note}")
        add("")
    return "\n".join(lines) if lines else "_No mismatches._\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_engine() -> engine.DeletionAuditEngine:
    """Construct and load the deterministic engine against the dataset."""
    audit = engine.DeletionAuditEngine()
    audit.load_policy()
    return audit


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--requests", type=Path, default=config.SAMPLE_REQUESTS_CSV)
    parser.add_argument("--gold", type=Path, default=config.SAMPLE_OUTPUT_CSV)
    parser.add_argument("--out", type=Path, default=None, help="write the markdown report here")
    parser.add_argument("--show-mismatches", action="store_true", help="print per-case differences")
    args = parser.parse_args(argv)

    requests = load_requests(args.requests)
    gold = load_gold(args.gold)
    report = score(build_engine(), requests, gold)

    rendered = render_markdown(report, args.requests, args.gold)
    print(rendered)

    if args.show_mismatches:
        print()
        print("## Mismatch detail")
        print()
        print(render_mismatches(report))

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding=config.CSV_ENCODING, newline=config.CSV_NEWLINE)
        print(f"\nwrote {args.out}")

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
