"""Opt-in, durable Power Pool Quick-to-Full research pilot.

Pilot limits are enforced using the study and its trial rows. A Full promotion
is reserved by inserting a PRUNED/PENDING_FULL trial *before* enqueue, so a
restart cannot promote the same Quick candidate twice. No submission API is used.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from ..brain.schemas import FULL_MODE
from ..db.models import Study, Trial, TrialState
from .fastexpr import ParseError, parse, render
from .params import POWER_POOL_SAMPLER, PowerPoolParams, params_of

if TYPE_CHECKING:
    from .study import Optimizer

MAX_QUICK = 10
MAX_FULL = 2
MAX_LLM = 2


@dataclass(frozen=True, slots=True)
class Choice:
    number: int
    dataset: str
    identity: str
    quality: float


def _identity(expression: str) -> str:
    try:
        return render(parse(expression))
    except ParseError:
        return expression.strip()


def _metric(value: object) -> float:
    try:
        result = float(value) if value is not None else float("nan")
    except TypeError, ValueError:
        return float("nan")
    return result if math.isfinite(result) else float("nan")


def candidates(trials: list[Trial]) -> list[Choice]:
    """Select only BRAIN-passing Quick results with positive Sharpe and Fitness.

    A valid Quick candidate is not a submission-worthy Full alpha. Require both
    basic performance metrics, a BRAIN Quick-check pass, and distinct expressions.
    Selection uses Fitness first and Sharpe second, never Sharpe alone.
    """
    from .study import passed_quick

    best: dict[str, Choice] = {}
    for trial in trials:
        if trial.state != TrialState.COMPLETE or not trial.expression:
            continue
        result = trial.result or {}
        if not passed_quick(result):
            continue
        stats = result.get("stats") or {}
        sharpe = _metric(stats.get("sharpe"))
        fitness = _metric(stats.get("fitness"))
        if not math.isfinite(sharpe) or not math.isfinite(fitness):
            continue
        if sharpe <= 0 or fitness <= 0:
            continue
        identity = _identity(trial.expression)
        choice = Choice(
            number=trial.number,
            dataset=str((trial.params or {}).get("dataset") or "unknown"),
            identity=identity,
            quality=fitness + 0.1 * sharpe,
        )
        existing = best.get(identity)
        if existing is None or (choice.quality, -choice.number) > (
            existing.quality,
            -existing.number,
        ):
            best[identity] = choice
    return sorted(best.values(), key=lambda c: (-c.quality, c.number))


def choose_diverse(ranked: list[Choice], count: int) -> list[Choice]:
    """Prefer different datasets without choosing a weaker duplicate expression."""
    if count <= 0:
        return []
    chosen: list[Choice] = []
    used_datasets: set[str] = set()
    for choice in ranked:
        if choice.dataset not in used_datasets:
            chosen.append(choice)
            used_datasets.add(choice.dataset)
            if len(chosen) >= count:
                return chosen
    for choice in ranked:
        if choice not in chosen:
            chosen.append(choice)
            if len(chosen) >= count:
                break
    return chosen


async def stage_promotions(optimizer: Optimizer, study_id: int) -> int:
    """Reserve up to two Full reruns, atomically with existing trial state.

    Must run inside the per-study optimizer lock held by Optimizer.advance.
    Trial rows, not an in-memory counter, are the source of truth after restart.
    """
    async with optimizer.db.session() as session:
        study = await session.get(Study, study_id)
        if study is None or study.sampler != POWER_POOL_SAMPLER:
            return 0
        run = params_of(study, PowerPoolParams)
        if not run.pilot or run.stopping:
            return 0
        cap = min(MAX_FULL, run.pilot_full_cap)
        if cap <= 0:
            return 0
        rows = list(
            (
                await session.scalars(
                    select(Trial).where(Trial.study_id == study_id).order_by(Trial.number)
                )
            ).all()
        )
        existing = [
            t for t in rows if (t.params or {}).get("fullOf") is not None
        ]
        remaining = cap - len(existing)
        if remaining <= 0:
            return 0
        already = {int(t.params["fullOf"]) for t in existing}
        ranked = [c for c in candidates(rows) if c.number not in already]
        to_stage = choose_diverse(ranked, remaining)
        if not to_stage:
            return 0
        by_number = {t.number: t for t in rows}
        last = max((t.number for t in rows), default=-1)
        for choice in to_stage:
            quick = by_number[choice.number]
            last += 1
            session.add(
                Trial(
                    study_id=study_id,
                    number=last,
                    params={
                        "fullOf": quick.number,
                        "pilotPromotion": True,
                        "dataset": choice.dataset,
                        "quality": choice.quality,
                    },
                    distributions={},
                    expression=quick.expression,
                    settings={**(quick.settings or {}), "simulationMode": FULL_MODE},
                    state=TrialState.PRUNED,
                    message="Waiting to run in Full mode.",
                )
            )
        await session.commit()
        return len(to_stage)
