"Observed research evidence. Missing observations are never interpreted as zero."

import hashlib
import math
from dataclasses import asdict, replace
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import numpy as np

from ..campaign_review import ExperimentPlan, ResearchDesign, review_candidates
from ..labs.fastexpr import Node, operator_count, parse, render, walk
from ..vault import metrics
from ..vault.yields import checks_of, verdict

if TYPE_CHECKING:
    from .schemas import CampaignSpec


def identities(expression: str, names: frozenset[str]) -> tuple[str, str, int]:
    tree = parse(expression)

    def shape(node: Node) -> Node:
        return replace(
            node,
            value="N"
            if node.kind == "num"
            else "FIELD"
            if node.kind == "name" and node.value in names
            else node.value,
            args=tuple(shape(arg) for arg in node.args),
            kwargs=tuple((key, shape(arg)) for key, arg in node.kwargs),
        )

    return (
        hashlib.sha256(render(tree).encode()).hexdigest(),
        hashlib.sha256(render(shape(tree)).encode()).hexdigest(),
        operator_count(tree),
    )


def usable(series: dict[Any, Any]) -> dict[date, tuple[float, float]]:
    return {
        day: (float(pair[0]), float(pair[1]))
        for day, pair in series.items()
        if isinstance(day, date)
        and isinstance(pair, tuple | list)
        and len(pair) == 2
        and all(
            isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)
            for v in pair
        )
        and pair[1] >= 0
    }


def correlation(
    left: dict[date, tuple[float, float]],
    right: dict[date, tuple[float, float]],
    spec: CampaignSpec,
) -> tuple[float | None, int]:
    end = spec.validation_start - timedelta(days=spec.embargo_days)
    left, right = usable(left), usable(right)
    days = sorted(d for d in left.keys() & right.keys() if d < end)
    if len(days) < spec.min_observations:
        return None, len(days)
    a = np.array(
        [left[d][0] - left[d][1] * metrics.BOOK_SIZE * spec.cost_bps / 10_000 for d in days]
    )
    b = np.array(
        [right[d][0] - right[d][1] * metrics.BOOK_SIZE * spec.cost_bps / 10_000 for d in days]
    )
    if a.std() == 0 or b.std() == 0:
        return None, len(days)
    value = float(np.corrcoef(a, b)[0, 1])
    return (value if math.isfinite(value) else None), len(days)


def _stats(
    series: dict[date, tuple[float, float]], days: list[date], bps: float
) -> dict[str, Any] | None:
    if len(days) < 60:
        return None
    pnl = np.array([series[d][0] for d in days])
    turnover = np.array([series[d][1] for d in days])
    result = metrics.stats(metrics.after_cost(pnl, turnover, bps), turnover, trim=False)
    if result is None:
        return None
    return {
        k: v if v is None or (isinstance(v, int | float) and math.isfinite(v)) else None
        for k, v in asdict(result).items()
    }


def covariance_estimate(
    ids: list[str], series: dict[str, dict[date, tuple[float, float]]], spec: CampaignSpec
) -> dict[str, Any]:
    clean = {key: usable(series.get(key, {})) for key in ids}
    if not ids:
        return {"available": False, "reason": "No explicitly frozen submitted baseline."}
    common = set.intersection(*(set(clean[key]) for key in ids))
    days = sorted(d for d in common if spec.validation_start <= d <= spec.validation_end)
    if len(days) < spec.min_observations:
        return {
            "available": False,
            "observations": len(days),
            "reason": "Insufficient aligned validation observations.",
        }
    values = (
        np.array(
            [
                [
                    clean[key][d][0] - clean[key][d][1] * metrics.BOOK_SIZE * spec.cost_bps / 10_000
                    for key in ids
                ]
                for d in days
            ]
        )
        / metrics.BOOK_SIZE
    )
    cov = np.atleast_2d(np.cov(values, rowvar=False))
    shrunk = 0.8 * cov + 0.2 * np.diag(np.diag(cov))
    ridge = max(float(np.trace(shrunk)) / len(ids) * 0.01, 1e-12)
    weights = np.linalg.solve(shrunk + np.eye(len(ids)) * ridge, np.ones(len(ids)))
    weights = np.maximum(weights, 0)
    weights = weights / weights.sum() if weights.sum() > 0 else np.full(len(ids), 1 / len(ids))
    returns = values @ weights
    sd = float(returns.std(ddof=1))
    return {
        "available": True,
        "observations": len(days),
        "shrinkage": 0.2,
        "regularization": ridge,
        "weights": dict(zip(ids, weights.tolist(), strict=True)),
        "estimatedSharpe": float(returns.mean() / sd * math.sqrt(metrics.TRADING_DAYS))
        if sd
        else None,
        "label": (
            "Historical covariance estimate on aligned validation dates; not untouched performance."
        ),
    }


def assess(
    spec: CampaignSpec,
    alpha_id: str,
    stored: dict[str, dict[str, Any]],
    series: dict[str, dict[date, tuple[float, float]]],
    submitted: list[dict[str, Any]],
    tested: int,
    *,
    reused_window: bool,
    created_before_validation: bool,
    other_selected: list[str],
    pool_rows: dict[str, dict[str, Any]] | None = None,
    sensitivity: list[dict[str, Any]] | None = None,
    field_coverage: dict[str, float | None] | None = None,
) -> dict[str, Any]:
    if alpha_id in spec.baseline_ids:
        return {
            "fullEligibilityObserved": False,
            "synthetic": spec.mode == "offline",
            "recommendation": "needs_evidence_or_reject",
            "confidence": "insufficient",
            "failureClass": "duplicate",
            "reasons": ["Candidate reuses an alpha in the frozen submitted baseline."],
            "submissionReady": False,
        }
    clean = {key: usable(value) for key, value in series.items()}
    plan = ExperimentPlan(
        **ResearchDesign.model_validate(spec).model_dump(), candidate_ids=[alpha_id]
    )
    review = review_candidates(plan, stored, submitted, clean, pool_rows or {})[0]
    result = review.model_dump(mode="json")
    reasons = list(review.reasons)
    row = stored.get(alpha_id, {})
    recorded = checks_of(row.get("checks"))
    risk_checks = {
        name: next((c.get("result") for c in recorded if c.get("name") == name), None)
        for name in ["CONCENTRATED_WEIGHT", "LOW_SUB_UNIVERSE_SHARPE"]
    }
    if any(value != "PASS" for value in risk_checks.values()):
        reasons.append("Recorded concentration and subuniverse PASS evidence is required.")
    field_coverage = field_coverage or {}
    if not field_coverage or any(
        value is None or not math.isfinite(value) or value <= 0 for value in field_coverage.values()
    ):
        reasons.append("Positive cached field coverage is incomplete or unknown.")
    full_eligible = (
        row.get("simulation_mode") == "FULL"
        and verdict(checks_of(row.get("checks")), "FULL") == "submittable"
    )
    daily = clean.get(alpha_id, {})
    validation = sorted(d for d in daily if spec.validation_start <= d <= spec.validation_end)
    regimes: list[dict[str, Any]] = []
    for offset in range(0, len(validation), 90):
        days = validation[offset : offset + 90]
        if len(days) >= spec.min_observations:
            regimes.append(
                {
                    "start": days[0].isoformat(),
                    "end": days[-1].isoformat(),
                    "days": len(days),
                    "stats": _stats(daily, days, spec.cost_bps),
                }
            )
    correlation_rows: list[dict[str, Any]] = []
    for baseline in [*spec.baseline_ids, *other_selected]:
        value, count = correlation(daily, clean.get(baseline, {}), spec)
        correlation_rows.append({"alphaId": baseline, "correlation": value, "days": count})
        if value is None:
            reasons.append(f"Insufficient discovery correlation evidence against {baseline}.")
        elif abs(value) > spec.max_discovery_correlation:
            reasons.append(
                f"Discovery correlation exceeds the configured ceiling against {baseline}."
            )
    untouched = spec.holdout_declared_untouched and created_before_validation and not reused_window
    robustness = bootstrap_evidence(daily, validation, spec, tested)
    if not robustness.get("passed"):
        reasons.append("Selection-adjusted block-bootstrap evidence is insufficient.")
    stress = _stats(daily, validation, spec.cost_bps * 2)
    if stress is None or (stress.get("sharpe") or 0) <= 0:
        reasons.append("Positive performance at twice the declared costs is unproven.")
    if len(regimes) < 2 or any(
        not r["stats"] or (r["stats"].get("sharpe") or 0) <= 0 for r in regimes
    ):
        reasons.append("Positive performance across at least two validation regimes is unproven.")
    sensitivity = sensitivity or []
    if len(sensitivity) < 2 or not all(s.get("passed") for s in sensitivity):
        reasons.append(
            "At least two independently simulated neighboring parameters must pass Quick checks."
        )
    stats = _stats(daily, validation, spec.cost_bps)
    measured_failure = bool(stats and stats.get("sharpe") is not None and stats["sharpe"] <= 0)
    excessive_correlation = any(
        r["correlation"] is not None and abs(r["correlation"]) > spec.max_discovery_correlation
        for r in correlation_rows
    )
    failure_class = (
        "weak_performance"
        if measured_failure
        else "excessive_correlation"
        if excessive_correlation
        else "constraint_violation"
        if any(value == "FAIL" for value in risk_checks.values())
        else "insufficient_evidence"
        if reasons
        else None
    )
    unavailable = [
        (
            "Quick parameter sensitivity is preliminary; neighboring Full "
            "results may still be needed."
        ),
        ("Coverage/concentration and subuniverse claims require recorded platform evidence."),
    ]
    if not untouched:
        unavailable.append(
            "Validation is exploratory/reused or not prospectively frozen; "
            "not untouched out-of-sample."
        )
    result |= {
        "fullEligibilityObserved": full_eligible,
        "failureClass": failure_class,
        "synthetic": spec.mode == "offline",
        "correlations": correlation_rows,
        "regimeWindows": regimes,
        "validationStats": _stats(daily, validation, spec.cost_bps),
        "costStressStats": stress,
        "parameterSensitivity": sensitivity,
        "riskChecks": risk_checks,
        "cachedFieldCoverage": field_coverage,
        "untouchedHoldout": untouched,
        "reusedWindow": reused_window,
        "excludedIncompleteObservations": len(series.get(alpha_id, {})) - len(daily),
        "unavailable": unavailable,
        "multipleTesting": robustness,
        "portfolioEstimate": covariance_estimate([*spec.baseline_ids, alpha_id], clean, spec),
        "reasons": list(dict.fromkeys(reasons)),
        "confidence": "supported_for_manual_review"
        if not reasons and full_eligible
        else "insufficient",
        "recommendation": "approve_for_manual_review"
        if not reasons and full_eligible
        else "needs_evidence_or_reject",
        "submissionReady": False,
    }
    return result


def bootstrap_evidence(
    series: dict[date, tuple[float, float]], days: list[date], spec: CampaignSpec, tested: int
) -> dict[str, Any]:
    """Deterministic moving-block bootstrap with conservative Bonferroni reporting.

    Assumes local stationarity within five observations; this diagnostic cannot turn a
    reused period into an independent holdout or account for unrecorded external trials.
    """
    result: dict[str, Any] = {
        "expressionsTried": tested,
        "passed": False,
        "method": ("Five-observation moving-block bootstrap; Bonferroni over recorded expressions"),
        "iterations": 2048,
        "blockLength": 5,
        "assumptions": (
            "Local stationarity; recorded search count only; exploratory when windows are reused."
        ),
    }
    if len(days) < spec.min_observations:
        return result | {"reason": "Insufficient aligned observations."}
    values = np.array(
        [series[d][0] - series[d][1] * metrics.BOOK_SIZE * spec.cost_bps / 10_000 for d in days]
    )
    if not np.isfinite(values).all() or values.std() == 0:
        return result | {"reason": "Missing or constant net returns."}
    rng = np.random.default_rng(20261122)
    starts = rng.integers(0, len(values) - 4, (2048, math.ceil(len(values) / 5)))
    indices = (starts[:, :, None] + np.arange(5)).reshape(2048, -1)[:, : len(values)]
    means = values[indices].mean(axis=1)
    observed = float(values.mean())
    p = float((1 + np.count_nonzero(means - observed >= observed)) / 2049)
    adjusted = min(1.0, p * max(1, tested))
    lower = float(np.quantile(means, 0.05 / max(1, tested)))
    return result | {
        "observations": len(days),
        "rawP": p,
        "adjustedP": adjusted,
        "adjustedLowerMeanPnl": lower,
        "passed": lower > 0 and adjusted <= 0.05,
        "statisticallyAdjusted": True,
    }


def parameter_neighbor(left: str, right: str) -> bool:
    """Same fields/operators with a small, nonzero numeric perturbation only."""
    a, b = parse(left), parse(right)
    left_nodes = [node for _, node in walk(a)]
    right_nodes = [node for _, node in walk(b)]
    if len(left_nodes) != len(right_nodes):
        return False
    changes = 0
    for x, y in zip(left_nodes, right_nodes, strict=True):
        if (
            x.kind != y.kind
            or len(x.args) != len(y.args)
            or tuple(k for k, _ in x.kwargs) != tuple(k for k, _ in y.kwargs)
        ):
            return False
        if x.kind != "num":
            if x.value != y.value:
                return False
        elif x.value != y.value:
            try:
                first, second = float(x.value), float(y.value)
            except ValueError:
                return False
            if abs(first - second) > max(5, abs(first) * 0.5):
                return False
            changes += 1
    return 0 < changes <= 2
