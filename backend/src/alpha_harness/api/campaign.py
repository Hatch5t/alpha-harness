"""Q4 campaign status, local catalog leads and research review."""

from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Query

from ..brain.errors import BrainError
from ..brain.filters import PLATFORM_TZ
from ..campaign_rules import (
    EXPERT_PYRAMIDS,
    EXPERT_SIGNALS,
    POWER_POOL_DEADLINE,
    Milestone,
    PyramidPriority,
    in_q4,
    milestones,
    rank_pyramids,
)
from ..catalog.pyramids import pyramid_grid
from ..catalog.queries import FieldFilter
from ..schemas import Out
from .catalog import DataFieldRow, Scope
from .deps import State
from .quarter import read_standing

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/campaign", tags=["campaign"])


class CampaignSummary(Out):
    quarter: str
    as_of: str
    active: bool
    submitted_signals: int | None = None
    completed_pyramids: int | None = None
    planning_signals_target: int = EXPERT_SIGNALS
    planning_pyramids_target: int = EXPERT_PYRAMIDS
    pyramid_candidates: list[PyramidPriority] | None = None
    milestones: list[Milestone]


@router.get("")
async def summary(state: State) -> CampaignSummary:
    today = datetime.now(PLATFORM_TZ).date()
    result = CampaignSummary(
        quarter="2026-Q4",
        as_of=today.isoformat(),
        active=in_q4(today),
        milestones=milestones(today),
    )
    if not result.active or not state.auth.session.authenticated:
        return result
    quarter = await read_standing(state, today)
    result.submitted_signals = quarter.submitted
    result.completed_pyramids = quarter.pyramids_formulated
    if quarter.pyramids_formulated is None:
        return result
    try:
        grid = await pyramid_grid(
            state.endpoints, state.catalog, account=state.auth.session.user_id, today=today
        )
    except BrainError:
        log.warning("campaign.pyramid_grid_unavailable")
    else:
        result.pyramid_candidates = rank_pyramids(
            grid, prioritize_theme=today <= POWER_POOL_DEADLINE
        )
    return result


class UnusedFields(Out):
    synced: bool
    synced_at: datetime | None
    total: int
    results: list[DataFieldRow]


@router.get("/unused-fields")
async def unused_fields(
    state: State,
    scope: Scope,
    min_coverage: Annotated[float, Query(ge=0, le=1)] = 0.7,
    min_date_coverage: Annotated[float, Query(ge=0, le=1)] = 0.6,
    dataset: Annotated[str | None, Query(max_length=80)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 30,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> UnusedFields:
    scopes = await state.queries.synced_tuples()
    match = next(
        (
            s
            for s in scopes
            if s["instrument_type"] == scope.instrument_type
            and s["region"] == scope.region
            and s["delay"] == scope.delay
            and s["universe"] == scope.universe
        ),
        None,
    )
    if match is None:
        return UnusedFields(synced=False, synced_at=None, total=0, results=[])
    found = await state.queries.fields(
        scope,
        FieldFilter(
            dataset_ids=[dataset] if dataset else [],
            alpha_count_min=0,
            alpha_count_max=0,
            coverage_min=min_coverage,
            date_coverage_min=min_date_coverage,
            sort_by="coverage",
            sort_desc=True,
            limit=limit,
            offset=offset,
        ),
    )
    return UnusedFields.model_validate(found | {"synced": True, "syncedAt": match["synced_at"]})
