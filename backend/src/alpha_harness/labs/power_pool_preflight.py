"""Pre-simulation Power Pool safeguards.

Selection is deterministic, discovery-only and never consults BRAIN simulation scores.
It uses syntax to reduce redundant *mechanisms*, not to predict return or Sharpe.
Original model expressions are retained when a narrow repair is made.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import TYPE_CHECKING

from .fastexpr import ParseError, node_at, parse, render, replace_at, walk

if TYPE_CHECKING:
    from ..db.models import Trial

#: Only these unary-on-data time-series operators are safe to move inside a
#: vector aggregator. Arbitrary vector arithmetic or binary operators are NOT repaired.
_SCALAR_TIMESERIES = frozenset(
    {
        "ts_delta",
        "ts_zscore",
        "ts_rank",
        "ts_mean",
        "ts_std_dev",
        "ts_decay_linear",
        "ts_av_diff",
    }
)
_VECTOR_AGGREGATORS = frozenset({"vec_avg", "vec_sum"})
_REVISION_TERMS = ("revision", "revising", "change", "acceleration", "momentum")
_CHANGE_OPERATORS = frozenset({"ts_delta", "ts_av_diff"})
_TIME_OPERATORS = frozenset(
    {
        *_SCALAR_TIMESERIES,
        "ts_corr",
        "ts_covariance",
        "ts_std_dev",
        "ts_sum",
    }
)


def repair_temporal_vector(expression: str, types: dict[str, str]) -> str:
    """Move a VECTOR aggregator just inside a unary time-series operation.

    Example: `rank(vec_avg(ts_delta(v, 20)))` becomes
    `rank(ts_delta(vec_avg(v), 20))`, but only when `v` is a known VECTOR.
    This is a structural repair, not evidence of equivalent economic behavior.
    Unsupported shapes stay unchanged for the normal validator to reject.
    """
    try:
        tree = parse(expression)
    except ParseError:
        return expression

    # Work bottom-up so paths remain valid after replacing an inner subtree.
    for path, _ in reversed(walk(tree)):
        node = node_at(tree, path)
        if node.kind != "call" or node.value not in _VECTOR_AGGREGATORS:
            continue
        if len(node.args) != 1 or node.kwargs:
            continue
        child = node.args[0]
        if child.kind != "call" or child.value not in _SCALAR_TIMESERIES or not child.args:
            continue
        field = child.args[0]
        if field.kind != "name" or types.get(field.value) != "VECTOR":
            continue
        aggregation = replace(node, args=(field,))
        moved = replace(child, args=(aggregation, *child.args[1:]))
        tree = replace_at(tree, path, moved)
    return render(tree)


def prefill_calls(max_trials: int, dataset_ids: list[str]) -> int:
    """Small up-front exploration across datasets, capped to three LLM calls.

    Ten simulations over many datasets require two successful generation calls,
    rather than letting the first dataset consume all ten. No billing is initiated
    here: the caller's existing LLM budget and rate limiter remain authoritative.
    """
    if max_trials <= 0:
        return 0
    return min(len(set(dataset_ids)), 3, max(1, (max_trials + 4) // 5))


def _profile(expression: str) -> tuple[str, frozenset[str]]:
    try:
        tree = parse(expression)
    except ParseError:
        return "unparsed", frozenset()
    calls = frozenset(node.value for _, node in walk(tree) if node.kind == "call")
    # Prefer a meaningful economic mechanism over an incidental outer rank().
    for operator, family in (
        ("ts_delta", "revision"),
        ("ts_av_diff", "relative-change"),
        ("ts_corr", "association"),
        ("ts_zscore", "anomaly"),
        ("group_rank", "peer-relative"),
        ("ts_rank", "historical-rank"),
        ("ts_decay_linear", "smoothed"),
        ("ts_mean", "trend"),
    ):
        if operator in calls:
            return family, calls
    return "static-level", calls


def _priority(expression: str, hypothesis: str) -> int:
    family, operators = _profile(expression)
    wants_change = any(term in hypothesis.lower() for term in _REVISION_TERMS)
    score = 0
    if operators & _TIME_OPERATORS:
        score += 3
    if operators & _CHANGE_OPERATORS:
        score += 4 if wants_change else 2
    if "group_rank" in operators:
        score += 2
    if family == "static-level":
        score -= 5
    # Prefer parsimonious expressions among those testing the same mechanism.
    return score - max(0, len(operators) - 4)


def select_pending(ready: list[Trial], want: int, hypothesis: str) -> list[Trial]:
    """Choose unsimulated expressions across datasets and structural families.

    For each pass, pick at most one expression per available dataset, preferring
    previously unselected operator families. Every tie breaks by trial number.
    This is a pre-simulation allocation heuristic, not an estimated quality score.
    """
    if want <= 0:
        return []
    buckets: dict[str, list[Trial]] = defaultdict(list)
    for trial in ready:
        dataset = str((trial.params or {}).get("dataset") or "unknown")
        buckets[dataset].append(trial)
    for candidates in buckets.values():
        candidates.sort(
            key=lambda trial: (-_priority(trial.expression or "", hypothesis), trial.number)
        )

    datasets = sorted(buckets)
    selected: list[Trial] = []
    families: set[tuple[str, str]] = set()
    while len(selected) < want and any(buckets.values()):
        for dataset in datasets:
            candidates = buckets[dataset]
            if not candidates or len(selected) >= want:
                continue
            index = next(
                (
                    i
                    for i, candidate in enumerate(candidates)
                    if (dataset, _profile(candidate.expression or "")[0]) not in families
                ),
                0,
            )
            chosen = candidates.pop(index)
            selected.append(chosen)
            families.add((dataset, _profile(chosen.expression or "")[0]))
    return selected
