"""Local experiment history and the human review step for the Q4 campaign."""

import asyncio
from datetime import datetime

from fastapi import APIRouter
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..brain.filters import PLATFORM_TZ
from ..campaign_review import CandidateEvidence, ExperimentPlan, ReviewDecision, review_candidates
from ..db.models import CampaignExperiment, CampaignReview, utcnow
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
    stored, submitted, series, pool = await asyncio.gather(
        state.alphas.by_ids(ids),
        state.alphas.submitted_members(),
        state.alphas.series(ids),
        local_power_pool(state, plan.candidate_ids),
    )
    candidates = await asyncio.to_thread(
        review_candidates,
        plan,
        stored,
        submitted,
        series,
        {r.alpha_id: r.model_dump() for r in pool.rows},
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
