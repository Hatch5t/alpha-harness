"""Select a completed lab task's shortlist without consulting validation performance."""

import asyncio
import hashlib
import itertools
import json
import math
from collections import Counter
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pydantic import Field
from sqlalchemy import select

from .brain.schemas import QUICK_MODE
from .campaign_review import ResearchDesign, discovery_sharpe, identity
from .db.models import Trial, TrialState
from .labs import ga
from .schemas import Out
from .vault import metrics
from .vault.yields import is_submitted

if TYPE_CHECKING:
    from .campaign_review import Series
    from .db.models import Study
    from .state import AppState

CHUNK_SIZE = 200


class TaskReviewRequest(ResearchDesign):
    task_id: int = Field(gt=0)
    shortlist_size: int = Field(default=3, ge=1, le=6)
    max_discovery_correlation: float = Field(
        default=ga.DEFAULT_MAX_CORRELATION, gt=0, le=1, allow_inf_nan=False
    )


class SelectionRow(Out):
    alpha_id: str
    discovery_sharpe: float | None = None
    selected: bool = False
    reason: str | None = None
    closest_alpha_id: str | None = None
    discovery_correlation: float | None = None


class TaskSource(Out):
    task_id: int
    name: str
    sampler: str
    objectives: list[str]
    total_trials: int
    trial_states: dict[str, int]
    unique_alphas: int
    selected: int
    rejections: dict[str, int]
    trial_failures: dict[str, int]
    selection_note: str
    max_discovery_correlation: float


class SelectionPage(Out):
    total: int
    results: list[SelectionRow]


class SelectionNotReady(ValueError):
    """Some completed simulations need their Vault data synced first."""


def request_key(body: TaskReviewRequest) -> str:
    payload = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _fingerprint(row: dict[str, Any]) -> str | None:
    key = identity(row)
    if key is None:
        return None
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()


def _score(
    body: TaskReviewRequest,
    ids: tuple[str, ...],
    stored: dict[str, dict[str, Any]],
    series: Series,
    submitted: set[str],
) -> list[tuple[SelectionRow, str | None]]:
    results: list[tuple[SelectionRow, str | None]] = []
    for alpha_id in ids:
        row = stored.get(alpha_id)
        if row is None:
            raise SelectionNotReady(f"Sync Vault metadata and PnL with turnover for {alpha_id}.")
        result = SelectionRow(alpha_id=alpha_id)
        key = _fingerprint(row)
        if (row.get("region"), row.get("delay")) != (body.region, body.delay):
            result.reason = "Outside the planned market"
        elif is_submitted(row) or row.get("date_submitted") or alpha_id in body.baseline_ids:
            result.reason = "Already in the submitted inventory or baseline"
        elif row.get("simulation_mode") == QUICK_MODE:
            result.reason = "Quick simulation needs a full run"
        elif key is None:
            result.reason = "Expression identity unavailable"
        elif key in submitted:
            result.reason = "Expression matches the submitted inventory"
        else:
            if not series.get(alpha_id):
                raise SelectionNotReady(f"Sync daily PnL with turnover for {alpha_id}.")
            result.discovery_sharpe = discovery_sharpe(body, alpha_id, series)
            if result.discovery_sharpe is None:
                result.reason = "Insufficient usable discovery observations"
            elif result.discovery_sharpe <= 0:
                result.reason = "Non-positive discovery Sharpe after costs"
        results.append((result, key))
    return results


async def select_from_task(
    state: AppState, body: TaskReviewRequest, study: Study
) -> tuple[TaskSource, list[SelectionRow], list[dict[str, Any]]]:
    async with state.db.session() as session:
        trials = [
            dict(row)
            for row in (
                await session.execute(
                    select(
                        Trial.number,
                        Trial.alpha_id,
                        Trial.state,
                        Trial.expression,
                        Trial.settings,
                        Trial.message,
                        Trial.params,
                    )
                    .where(Trial.study_id == body.task_id)
                    .order_by(Trial.number)
                )
            ).mappings()
        ]
    if any(row["state"] in {TrialState.QUEUED, TrialState.RUNNING} for row in trials):
        raise SelectionNotReady("The task still has simulations in progress.")
    ids = list(
        dict.fromkeys(
            row["alpha_id"]
            for row in trials
            if row["alpha_id"] and row["state"] == TrialState.COMPLETE
        )
    )
    submitted = {
        key for row in await state.alphas.submitted_members() if (key := _fingerprint(row))
    }
    scores: list[tuple[SelectionRow, str | None]] = []
    for chunk in itertools.batched(ids, CHUNK_SIZE):
        stored, series = await asyncio.gather(
            state.alphas.by_ids(list(chunk)), state.alphas.series(list(chunk))
        )
        scores.extend(await asyncio.to_thread(_score, body, chunk, stored, series, submitted))
    scores.sort(
        key=lambda item: (
            item[0].discovery_sharpe is None,
            -(item[0].discovery_sharpe or 0),
            item[0].alpha_id,
        )
    )
    kept: dict[str, ga.Series | None] = {}
    seen: set[str] = set()
    for row, key in scores:
        if row.reason:
            continue
        if key in seen:
            row.reason = "Expression matches a higher-ranked task result"
        elif len(kept) == body.shortlist_size:
            row.reason = "Below the fixed shortlist size"
        else:
            daily = await state.alphas.series([row.alpha_id])
            if not daily.get(row.alpha_id):
                raise SelectionNotReady("Vault series changed during selection. Retry the review.")
            end = body.validation_start - timedelta(days=body.embargo_days)
            own = ga.series_of(
                {
                    day: pnl - turnover * metrics.BOOK_SIZE * body.cost_bps / 10_000
                    for day, (pnl, turnover) in daily.get(row.alpha_id, {}).items()
                    if day < end and math.isfinite(pnl) and math.isfinite(turnover)
                }
            )
            measured = {
                alpha_id: ga.correlation(own, other) if own and other else None
                for alpha_id, other in kept.items()
            }
            if any(value is None or not math.isfinite(value) for value in measured.values()):
                row.reason = "Insufficient common discovery observations for diversification"
            else:
                if measured:
                    closest = max(measured, key=lambda a: abs(measured[a] or 0))
                    row.closest_alpha_id = closest
                    row.discovery_correlation = measured[closest]
                if (
                    row.discovery_correlation is not None
                    and abs(row.discovery_correlation) >= body.max_discovery_correlation
                ):
                    row.reason = "Discovery correlation exceeds the shortlist ceiling"
                else:
                    row.selected = True
                    kept[row.alpha_id] = own
        if key:
            seen.add(key)
    rows = [row for row, _ in scores]
    source = TaskSource(
        task_id=study.id,
        name=study.label or study.name,
        sampler=study.sampler,
        objectives=study.objectives,
        total_trials=len(trials),
        trial_states=dict(Counter(str(row["state"]) for row in trials)),
        trial_failures=dict(
            Counter(
                row["message"] or "No recorded trial message"
                for row in trials
                if row["state"] == TrialState.FAIL
            )
        ),
        unique_alphas=len(ids),
        selected=len(kept),
        max_discovery_correlation=body.max_discovery_correlation,
        rejections=dict(Counter(row.reason for row in rows if row.reason)),
        selection_note=(
            "Ranked on discovery Sharpe after the planned cost, before validation and embargo. "
            "The lab's own optimization may have used validation dates. "
            "Check that history before treating this comparison as out-of-sample evidence."
        ),
    )
    return source, rows, trials
