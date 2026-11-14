"""Power Pool evidence from existing Vault, Portfolio and platform check data."""

import math
from datetime import date, timedelta
from typing import Annotated, Any, Literal, Self

import numpy as np
from pydantic import Field, StringConstraints, model_validator

from .brain.platform import IGNORED_CHECKS
from .labs import fastexpr
from .schemas import Out
from .vault import metrics, yields

MIN_WINDOW_ROWS = 60
AlphaId = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
type Series = dict[str, dict[date, tuple[float, float]]]


class ExperimentPlan(Out):
    hypothesis: str = Field(min_length=12, max_length=4000)
    candidate_ids: list[AlphaId] = Field(min_length=1, max_length=6)
    baseline_ids: list[AlphaId] = Field(default_factory=list, max_length=24)
    validation_start: date
    validation_end: date
    embargo_days: int = Field(default=5, ge=0, le=365)
    cost_bps: float = Field(default=5, ge=0, le=100, allow_inf_nan=False)
    expected_theme: str | None = Field(default=None, min_length=1, max_length=240)

    @model_validator(mode="after")
    def validate_design(self) -> Self:
        ids = self.candidate_ids + self.baseline_ids
        if len(set(ids)) != len(ids):
            raise ValueError("Use each alpha ID once across candidates and baseline.")
        if (self.validation_end - self.validation_start).days < MIN_WINDOW_ROWS - 1:
            raise ValueError("The validation window must allow at least 60 observations.")
        if self.validation_start <= date.min + timedelta(days=self.embargo_days):
            raise ValueError("The embargo would place discovery outside the supported calendar.")
        if not self.hypothesis.strip():
            raise ValueError("An economic hypothesis is required.")
        return self


class CandidateEvidence(Out):
    alpha_id: str
    sharpe: float | None = None
    fitness: float | None = None
    turnover: float | None = None
    discovery_sharpe: float | None = None
    holdout_sharpe: float | None = None
    holdout_days: int = 0
    portfolio_days: int = 0
    holdout_delta: float | None = None
    pool_correlation: float | None = None
    pool_verdict: str | None = None
    amer_check: str | None = None
    theme_check: str | None = None
    duplicate_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


def identity(row: dict[str, Any]) -> tuple[str, str] | None:
    expression = row.get("expression")
    if not expression or row.get("sim_type") == "SUPER":
        return None
    language = str(row.get("language") or "FASTEXPR")
    if language == "FASTEXPR":
        try:
            expression = fastexpr.render(fastexpr.parse(expression)).rstrip(";")
        except fastexpr.ParseError, RecursionError:
            expression = expression.strip()
    return language, expression


def _figure(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and math.isfinite(value) else None


def _sharpe(series: Series, ids: list[str], days: list[date], bps: float) -> float | None:
    if len(days) < MIN_WINDOW_ROWS:
        return None
    pnl_turnover = np.array([[series[a][d] for a in ids] for d in days])
    pnl, turnover = pnl_turnover.mean(axis=1).T
    result = metrics.stats(metrics.after_cost(pnl, turnover, bps), turnover, trim=False)
    return _figure(result.sharpe) if result else None


def _finite_days(s: dict[date, tuple[float, float]]) -> set[date]:
    return {
        day for day, (pnl, turnover) in s.items() if math.isfinite(pnl) and math.isfinite(turnover)
    }


def review_candidates(
    plan: ExperimentPlan,
    stored: dict[str, dict[str, Any]],
    submitted: list[dict[str, Any]],
    series: Series,
    pool_rows: dict[str, dict[str, Any]],
) -> list[CandidateEvidence]:
    indexed: dict[tuple[str, str], list[str]] = {}
    for row in [*submitted, *(stored[a] for a in plan.candidate_ids if a in stored)]:
        key = identity(row)
        if key:
            indexed.setdefault(key, []).append(str(row["alpha_id"]))

    evidence: list[CandidateEvidence] = []
    for alpha_id in plan.candidate_ids:
        row = stored.get(alpha_id, {})
        out = CandidateEvidence(alpha_id=alpha_id)
        evidence.append(out)
        if not row:
            out.reasons.append("Alpha is missing from the local Vault.")
            continue
        out.sharpe = _figure(row.get("sharpe"))
        out.fitness = _figure(row.get("fitness"))
        out.turnover = _figure(row.get("turnover"))
        key = identity(row)
        if key:
            out.duplicate_ids = sorted(set(indexed[key]) - {alpha_id})
        else:
            out.reasons.append("Expression identity is unavailable for duplicate screening.")
        if out.duplicate_ids:
            out.reasons.append("Expression matches a submitted or shortlisted alpha.")
        if yields.is_submitted(row) or row.get("date_submitted") is not None:
            out.reasons.append("Candidate is already submitted.")
        if (row.get("region"), row.get("delay")) != ("GLB", 1):
            out.reasons.append("Candidate is outside GLB D1.")
        checks = yields.checks_of(row.get("checks"))
        if yields.verdict(checks, row.get("simulation_mode")) != "submittable":
            out.reasons.append("Stored BRAIN submission checks are incomplete or refused.")
            out.reasons.extend(
                f"{c['name']} {c['result']}"
                for c in checks
                if c.get("name")
                and c.get("name") not in IGNORED_CHECKS
                and c.get("result") in {"FAIL", "ERROR", "PENDING"}
            )
        amer = next((c for c in checks if c.get("name") == "LOW_GLB_AMER_SHARPE"), {})
        out.amer_check = amer.get("result")
        if out.amer_check != "PASS":
            out.reasons.append("AMER Sharpe check has no recorded PASS.")
        expected = (plan.expected_theme or "").strip().casefold()
        for check in checks:
            if check.get("name") != "MATCHES_THEMES" or not expected:
                continue
            matches = check.get("themes") or check.get("matched")
            if isinstance(matches, list) and any(
                isinstance(match, dict)
                and any(
                    str(match.get(k, "")).strip().casefold() == expected for k in ("name", "id")
                )
                for match in matches
            ):
                out.theme_check = check.get("result")
                break
        if out.theme_check != "PASS":
            out.reasons.append("The planned BRAIN theme has no stored PASS.")
        pool = pool_rows.get(alpha_id, {})
        out.pool_correlation = _figure(pool.get("correlation"))
        out.pool_verdict = pool.get("verdict")
        if out.pool_verdict not in {"clear", "beats"}:
            out.reasons.append("Local Power Pool orthogonality is unmeasured or blocked.")
        _review_windows(plan, out, series)

    # The shortlist is ranked by discovery performance alone.
    evidence.sort(
        key=lambda c: (c.discovery_sharpe is None, -(c.discovery_sharpe or 0), c.alpha_id)
    )
    return evidence


def _review_windows(plan: ExperimentPlan, out: CandidateEvidence, series: Series) -> None:
    candidate = out.alpha_id
    candidate_days = _finite_days(series.get(candidate, {}))
    discovery_end = plan.validation_start - timedelta(days=plan.embargo_days)
    discovery = sorted(d for d in candidate_days if d < discovery_end)
    out.discovery_sharpe = _sharpe(series, [candidate], discovery, plan.cost_bps)
    validation = {d for d in candidate_days if plan.validation_start <= d <= plan.validation_end}
    out.holdout_days = len(validation)
    out.holdout_sharpe = _sharpe(series, [candidate], sorted(validation), plan.cost_bps)
    if out.discovery_sharpe is None:
        out.reasons.append("Discovery window has insufficient usable observations.")
    if out.holdout_sharpe is None or out.holdout_sharpe <= 0:
        out.reasons.append("Validation Sharpe is unavailable or non-positive after costs.")
    if not plan.baseline_ids:
        out.reasons.append("Incremental contribution requires a frozen submitted baseline.")
        return
    for alpha_id in plan.baseline_ids:
        validation.intersection_update(_finite_days(series.get(alpha_id, {})))
    days = sorted(validation)
    out.portfolio_days = len(days)
    before = _sharpe(series, plan.baseline_ids, days, plan.cost_bps)
    after = _sharpe(series, [*plan.baseline_ids, candidate], days, plan.cost_bps)
    if before is not None and after is not None:
        out.holdout_delta = after - before
    if out.holdout_delta is None or out.holdout_delta <= 0:
        out.reasons.append("No positive portfolio contribution measured on common dates.")


class ReviewDecision(Out):
    alpha_id: AlphaId
    decision: Literal["approve_for_manual_review", "reject"]
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=4000)]
