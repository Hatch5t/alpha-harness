"""Local experiment history and the human review step for the Q4 campaign."""

import asyncio
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..brain.filters import PLATFORM_TZ
from ..campaign_review import CandidateEvidence, ExperimentPlan, ReviewDecision, review_candidates
from ..campaign_selection import (
    SelectionNotReady,
    SelectionPage,
    TaskReviewRequest,
    TaskSource,
    request_key,
    select_from_task,
)
from ..db.models import CampaignExperiment, CampaignReview, Study, StudyStatus, utcnow
from ..labs.params import TASK_SAMPLERS
from ..schemas import Out
from .deps import State, refuse
from .lab_tasks import local_power_pool

router = APIRouter(prefix="/api/campaign/experiments", tags=["campaign"])


class SavedDecision(ReviewDecision):
    decided_at: datetime


class ReviewSnapshot(Out):
    created_at: datetime
    candidates: list[CandidateEvidence]
    decision: SavedDecision | None


class Experiment(Out):
    id: int
    created_at: datetime
    plan: ExperimentPlan
    prospective: bool
    source: TaskSource | None = None
    review: ReviewSnapshot | None = None


def _snapshot(row: CampaignReview) -> ReviewSnapshot:
    return ReviewSnapshot.model_validate(
        row.evidence | {"createdAt": row.created_at, "decision": row.decision}
    )


def _experiment(row: CampaignExperiment, review: CampaignReview | None = None) -> Experiment:
    plan = ExperimentPlan.model_validate(row.plan)
    return Experiment(
        id=row.id,
        created_at=row.created_at,
        plan=plan,
        prospective=row.created_at.astimezone(PLATFORM_TZ).date() < plan.validation_start,
        source=TaskSource.model_validate(row.source) if row.source else None,
        review=_snapshot(review) if review else None,
    )


@router.get("")
async def history(state: State, before: int | None = None) -> list[Experiment]:
    query = (
        select(CampaignExperiment, CampaignReview)
        .outerjoin(CampaignReview)
        .order_by(CampaignExperiment.id.desc())
        .limit(30)
    )
    if before is not None:
        query = query.where(CampaignExperiment.id < before)
    async with state.db.session() as session:
        rows = await session.execute(query)
        return [_experiment(row, review) for row, review in rows]


@router.post("", status_code=201)
async def freeze(plan: ExperimentPlan, state: State) -> Experiment:
    """Freeze a shortlist and its portfolio comparison before asking for a report."""
    if not plan.candidate_ids:
        raise refuse(422, "candidates_empty", "Choose at least one candidate alpha.")
    ids = [*plan.candidate_ids, *plan.baseline_ids]
    stored = await state.alphas.by_ids(ids)
    if missing := [a for a in ids if a not in stored]:
        raise refuse(422, "alphas_missing", "Sync Vault data for: " + ", ".join(missing))
    if any(not stored[a].get("date_submitted") for a in plan.baseline_ids):
        raise refuse(422, "baseline_invalid", "Every baseline alpha must be a submitted alpha.")
    async with state.db.session() as session:
        row = CampaignExperiment(plan=plan.model_dump(mode="json"))
        session.add(row)
        await session.flush()
        return _experiment(row)


@router.post("/from-task", status_code=201)
async def from_task(body: TaskReviewRequest, state: State) -> Experiment:
    """Freeze discovery-only selection from an existing, completed research-lab task."""
    key = request_key(body)
    async with state.optimizer.lock(body.task_id):
        async with state.db.session() as session:
            existing = await session.scalar(
                select(CampaignExperiment).where(CampaignExperiment.source_key == key)
            )
            if existing:
                return _experiment(existing)
            study = await session.get(Study, body.task_id)
            if study is None:
                raise refuse(404, "task_missing", "The research task was not found.")
            if study.sampler not in TASK_SAMPLERS or study.status != StudyStatus.COMPLETE:
                raise refuse(409, "task_unfinished", "Complete the research-lab task first.")
        baseline = await state.alphas.by_ids(body.baseline_ids)
        if any(not baseline.get(a, {}).get("date_submitted") for a in body.baseline_ids):
            raise refuse(422, "baseline_invalid", "Sync every submitted baseline alpha first.")
        try:
            source, rows, trials = await select_from_task(state, body, study)
        except SelectionNotReady as exc:
            raise refuse(422, "selection_incomplete", str(exc)) from exc
        plan = ExperimentPlan(
            **body.model_dump(exclude={"task_id", "shortlist_size", "max_discovery_correlation"}),
            candidate_ids=[row.alpha_id for row in rows if row.selected],
        )
        async with state.db.session() as session:
            row = CampaignExperiment(
                plan=plan.model_dump(mode="json"),
                source_key=key,
                source=source.model_dump(mode="json"),
                provenance={
                    "configuration": {
                        "template": study.template_source,
                        "params": study.sampler_params,
                        "seed": study.seed,
                        "maxTrials": study.max_trials,
                    },
                    "trials": trials,
                },
                selection=[item.model_dump(mode="json") for item in rows],
            )
            session.add(row)
            await session.flush()
            return _experiment(row)


@router.get("/{experiment_id}/selection")
async def selection(
    experiment_id: int,
    state: State,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 30,
) -> SelectionPage:
    async with state.db.session() as session:
        row = (
            await session.execute(
                select(CampaignExperiment.selection).where(CampaignExperiment.id == experiment_id)
            )
        ).first()
    if row is None:
        raise refuse(404, "experiment_missing", "Research experiment was not found.")
    rows = row[0] or []
    return SelectionPage(total=len(rows), results=rows[offset : offset + limit])


@router.get("/{experiment_id}/export")
async def export_experiment(experiment_id: int, state: State) -> JSONResponse:
    """Export the local inputs, trial history, selection and recorded review together."""
    async with state.db.session() as session:
        row = await session.get(CampaignExperiment, experiment_id)
        if row is None:
            raise refuse(404, "experiment_missing", "Research experiment was not found.")
        provenance, selection_rows = (
            await session.execute(
                select(CampaignExperiment.provenance, CampaignExperiment.selection).where(
                    CampaignExperiment.id == experiment_id
                )
            )
        ).one()
        review = await session.scalar(
            select(CampaignReview).where(CampaignReview.experiment_id == experiment_id)
        )
        content = _experiment(row, review).model_dump(mode="json") | {
            "provenance": provenance,
            "selection": selection_rows,
        }
    return JSONResponse(
        content,
        headers={"Content-Disposition": f'attachment; filename="experiment-{experiment_id}.json"'},
    )


@router.post("/{experiment_id}/evaluate")
async def evaluate(experiment_id: int, state: State) -> ReviewSnapshot:
    """Record one repeatable snapshot. Reevaluation returns the recorded result."""
    async with state.db.session() as session:
        experiment = await session.get(CampaignExperiment, experiment_id)
        if experiment is None:
            raise refuse(404, "experiment_missing", "Research experiment was not found.")
        existing = await session.scalar(
            select(CampaignReview).where(CampaignReview.experiment_id == experiment_id)
        )
        if existing:
            return _snapshot(existing)
        plan = ExperimentPlan.model_validate(experiment.plan)
    if plan.validation_end >= datetime.now(PLATFORM_TZ).date():
        raise refuse(422, "open_validation", "Evaluate after the validation end date has passed.")

    ids = [*plan.candidate_ids, *plan.baseline_ids]
    stored, submitted, series = await asyncio.gather(
        state.alphas.by_ids(ids),
        state.alphas.submitted_members(),
        state.alphas.series(ids),
    )
    pool = (
        await local_power_pool(state, plan.candidate_ids)
        if plan.purpose == "power_pool" and plan.candidate_ids
        else None
    )
    candidates = await asyncio.to_thread(
        review_candidates,
        plan,
        stored,
        submitted,
        series,
        {r.alpha_id: r.model_dump() for r in pool.rows} if pool else {},
    )
    try:
        async with state.db.session() as session:
            row = CampaignReview(
                experiment_id=experiment_id,
                evidence={"candidates": [c.model_dump(mode="json") for c in candidates]},
            )
            session.add(row)
            await session.flush()
            return _snapshot(row)
    except IntegrityError:
        async with state.db.session() as session:
            existing = await session.scalar(
                select(CampaignReview).where(CampaignReview.experiment_id == experiment_id)
            )
            if existing:
                return _snapshot(existing)
        raise


@router.post("/{experiment_id}/decision")
async def decide(experiment_id: int, decision: ReviewDecision, state: State) -> ReviewSnapshot:
    async with state.db.session() as session:
        review = await session.scalar(
            select(CampaignReview).where(CampaignReview.experiment_id == experiment_id)
        )
        if review is None:
            raise refuse(404, "review_missing", "Evaluate the experiment first.")
        if review.decision is not None:
            raise refuse(409, "review_decided", "This review already has a decision.")
        evidence = _snapshot(review)
        candidate = next((c for c in evidence.candidates if c.alpha_id == decision.alpha_id), None)
        if candidate is None:
            raise refuse(422, "candidate_missing", "Select an alpha from this experiment.")
        if decision.decision == "approve_for_manual_review" and candidate.reasons:
            raise refuse(
                422, "evidence_incomplete", "Resolve the candidate's research findings first."
            )
        saved = SavedDecision.model_validate(decision.model_dump() | {"decidedAt": utcnow()})
        updated = await session.scalar(
            update(CampaignReview)
            .where(CampaignReview.id == review.id, CampaignReview.decision.is_(None))
            .values(decision=saved.model_dump(mode="json"))
            .returning(CampaignReview)
        )
        if updated is None:
            raise refuse(409, "review_decided", "This review already has a decision.")
        return _snapshot(updated)
