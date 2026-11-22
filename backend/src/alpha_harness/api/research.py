"""Local research orchestration, evidence and explicit spending approvals."""

import hashlib
import json
from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy import select

from ..db.models import ResearchBudget, ResearchCampaign, ResearchProfile, utcnow
from ..labs.notebook_import import ImportRefused, preview_notebook
from ..research import budget, profiles
from ..research.schemas import (
    Authorization,
    CampaignSpec,
    CandidateDecision,
    GlobalApproval,
    Profile,
)
from ..schemas import Out
from .deps import State, refuse

router = APIRouter(prefix="/api/research", tags=["research"])


class Control(Out):
    action: Literal["start", "pause", "resume", "cancel"]


class Notebook(Out):
    text: str = Field(max_length=2_000_000)


class Settlement(Out):
    acknowledgement: Literal["I CHECKED BRAIN; SETTLE WITHOUT RESENDING"]


@router.get("/options")
async def options(state: State) -> dict[str, Any]:
    await state.research.initialize()
    async with state.db.session() as session:
        policies = list(await session.scalars(select(ResearchBudget)))
    return {
        "profiles": [p.model_dump(mode="json") for p in await profiles.listing(state.db)],
        "models": [{"ref": m.ref, "provider": m.provider, "id": m.id} for m in state.models.all()],
        "globalBudgets": {p.mode: p.limits for p in policies},
        "usageToday": {
            mode: await budget.usage(state.db, None, mode) for mode in ["offline", "live"]
        },
        "authenticated": state.auth.session.authenticated,
        "quickAllowed": state.engine.quick_allowed,
        "modelUtility": await state.research.utility(),
    }


@router.post("/preview")
async def preview(body: CampaignSpec, state: State) -> dict[str, Any]:
    return await state.research.preview(body)


@router.post("", status_code=201)
async def create(body: CampaignSpec, state: State) -> dict[str, int]:
    return {"id": await state.research.create(body)}


@router.get("")
async def listing(state: State) -> list[dict[str, Any]]:
    async with state.db.session() as session:
        rows = list(
            await session.scalars(
                select(ResearchCampaign).order_by(ResearchCampaign.id.desc()).limit(100)
            )
        )
    return [
        {
            "id": r.id,
            "name": r.name,
            "mode": r.mode,
            **await state.research.generation_outcome(r),
            "createdAt": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.put("/global-budget")
async def global_budget(body: GlobalApproval, state: State) -> dict[str, Any]:
    await state.research.initialize()
    async with state.db.session() as session:
        row = await session.get(ResearchBudget, "live")
        if row is None:
            raise refuse(409, "budget_missing", "Research budget initialization failed.")
        row.limits, row.approved_at = body.limits.model_dump(mode="json"), utcnow()
    return {
        "limits": body.limits.model_dump(mode="json"),
        "usageToday": await budget.usage(state.db, None, "live"),
    }


@router.put("/profiles/{profile_id}")
async def profile(profile_id: str, body: Profile, state: State) -> Profile:

    if profile_id != body.id:
        raise refuse(422, "profile_identity", "The profile ID is immutable.")
    async with state.db.session() as session:
        row = await session.get(ResearchProfile, profile_id)
        if row is None:
            session.add(ResearchProfile(id=profile_id, document=body.model_dump(mode="json")))
        else:
            row.document = body.model_dump(mode="json")
    return body


@router.post("/notebook-preview")
async def notebook(body: Notebook) -> dict[str, Any]:
    try:
        result = preview_notebook(body.text)
    except ImportRefused as exc:
        raise refuse(422, "notebook_refused", str(exc)) from None
    return {
        "sourceHash": hashlib.sha256(body.text.encode()).hexdigest(),
        **result.model_dump(mode="json"),
    }


@router.get("/{campaign_id}")
async def report(campaign_id: int, state: State) -> dict[str, Any]:
    return await state.research.report(campaign_id)


@router.get("/{campaign_id}/download")
async def download(campaign_id: int, state: State) -> JSONResponse:
    data = await state.research.report(campaign_id, complete=True)
    # Validate the export with strict JSON before responding. NaN must never become a
    # plausible numeric observation in a downloadable research record.
    json.dumps(data, allow_nan=False)
    return JSONResponse(
        data,
        headers={
            "Content-Disposition": f'attachment; filename="research-{campaign_id}.json"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/{campaign_id}/authorize")
async def authorize(campaign_id: int, body: Authorization, state: State) -> dict[str, Any]:
    await state.research.authorize(campaign_id, body.config_hash)
    return await state.research.report(campaign_id)


@router.post("/{campaign_id}/control")
async def control(campaign_id: int, body: Control, state: State) -> dict[str, Any]:
    await state.research.control(campaign_id, body.action)
    return await state.research.report(campaign_id)


@router.post("/{campaign_id}/refresh-evidence")
async def refresh(campaign_id: int, state: State) -> dict[str, Any]:
    await state.research.refresh_evidence(campaign_id)
    return await state.research.report(campaign_id)


@router.post("/{campaign_id}/settle-uncertain")
async def settle(campaign_id: int, body: Settlement, state: State) -> dict[str, Any]:
    if body.acknowledgement != "I CHECKED BRAIN; SETTLE WITHOUT RESENDING":
        raise refuse(422, "settlement_refused", "Inspect BRAIN before settling uncertain work.")
    await state.research.resolve_uncertain(campaign_id)
    return await state.research.report(campaign_id)


@router.post("/{campaign_id}/candidates/{candidate_id}/review")
async def decision(
    campaign_id: int, candidate_id: int, body: CandidateDecision, state: State
) -> dict[str, Any]:
    await state.research.decision(campaign_id, candidate_id, body.decision, body.note)
    return await state.research.report(campaign_id)
