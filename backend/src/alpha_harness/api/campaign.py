"""Read-only Q4 2026 campaign page: eligibility progress and field discovery.

Returns BRAIN-sourced values and stored catalog rows, never simulates or submits.
"""
import asyncio
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query

from ..brain.filters import PLATFORM_TZ
from ..catalog.pyramids import pyramid_grid
from ..catalog.queries import FieldFilter, Tuple4
from ..schemas import Out
from ..campaign_rules import (
    EXPERT_PYRAMIDS,
    EXPERT_SIGNALS,
    in_q4,
    milestones,
    rank_pyramids,
)
from .deps import State
from .quarter import standing

router = APIRouter(prefix="/api/campaign", tags=["campaign"])


class CampaignSummary(Out):
    quarter: str
    as_of: str
    active: bool
    submitted_signals: int
    completed_pyramids: int
    signals_to_expert: int
    pyramids_to_expert: int
    platform_performance_available: bool
    performance_note: str
    pyramid_candidates: list[dict[str, Any]]
    milestones: list[dict[str, str | bool]]


@router.get("")
async def summary(state: State) -> CampaignSummary:
    """Rank locally synced opportunities against counts returned by BRAIN.

    Combined Genius/Osmosis scores are NOT available in the Harness quarter API;
    never turn an equal-weight local Portfolio Sharpe into a Genius score.
    """
    today = datetime.now(PLATFORM_TZ).date()
    quarter, grid = await asyncio.gather(
        standing(state), pyramid_grid(state.endpoints, state.catalog)
    )
    return CampaignSummary(
        quarter=quarter.label,
        as_of=today.isoformat(),
        active=in_q4(today),
        submitted_signals=quarter.submitted,
        completed_pyramids=quarter.pyramids_formulated,
        signals_to_expert=max(0, EXPERT_SIGNALS - quarter.submitted),
        pyramids_to_expert=max(0, EXPERT_PYRAMIDS - quarter.pyramids_formulated),
        platform_performance_available=False,
        performance_note=(
            "Genius combined Alpha, Selected Alpha, Power Pool and Osmosis scores "
            "must be checked in the signed-in BRAIN Genius dashboard. This service "
            "does not infer them from simulations or the local Portfolio."
        ),
        pyramid_candidates=rank_pyramids(grid),
        milestones=milestones(today),
    )


class UnusedFields(Out):
    region: str
    delay: int
    universe: str
    synced: bool
    total: int
    results: list[dict[str, Any]]
    qualification_note: str


@router.get("/unused-fields")
async def unused_fields(
    state: State,
    region: str = Query(min_length=2, max_length=12),
    delay: int = Query(ge=0, le=1),
    universe: str = Query(min_length=2, max_length=40),
    min_coverage: float = Query(default=0.7, ge=0.0, le=1.0),
    min_date_coverage: float = Query(default=0.6, ge=0.0, le=1.0),
    limit: int = Query(default=50, ge=1, le=200),
) -> UnusedFields:
    """Find fields whose BRAIN Alpha Count was zero at the last catalog sync.

    A zero count is a lead, not proof of eligibility. Rules, dataset coverage,
    historical count changes, and the live theme check still control submission.
    """
    scopes = await state.queries.synced_tuples()
    synced = any(
        s.get("instrument_type") == "EQUITY"
        and s.get("region") == region
        and s.get("delay") == delay
        and s.get("universe") == universe
        and (s.get("fields") or 0) > 0
        for s in scopes
    )
    result: dict[str, Any] = {"total": 0, "results": []}
    if synced:
        result = await state.queries.fields(
            Tuple4(region=region, delay=delay, universe=universe),
            FieldFilter(
                alpha_count_min=0,
                alpha_count_max=0,
                coverage_min=min_coverage,
                date_coverage_min=min_date_coverage,
                sort_by="coverage",
                sort_desc=True,
                limit=limit,
            ),
        )
    columns = (
        "field_id", "dataset_id", "category_name", "description", "field_type",
        "coverage", "date_coverage", "alpha_count", "pyramid_multiplier"
    )
    return UnusedFields(
        region=region,
        delay=delay,
        universe=universe,
        synced=synced,
        total=int(result.get("total") or 0),
        results=[{k: row.get(k) for k in columns} for row in result.get("results") or []],
        qualification_note=(
            "Snapshot from the local field catalog. Alpha Count = 0 is not a "
            "guarantee of an unused-field multiplier. Refresh the catalog and "
            "verify eligibility on BRAIN before tagging/submission."
        ),
    )
