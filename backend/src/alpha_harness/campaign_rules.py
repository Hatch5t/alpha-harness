"""Calendar and local research priorities for the Q4 campaign."""

import math
from datetime import date
from typing import Any, Literal

from .schemas import Out

QUARTER_START = date(2026, 10, 1)
QUARTER_END = date(2026, 12, 31)
POWER_POOL_DEADLINE = date(2026, 11, 22)
EXPERT_SIGNALS = 20
EXPERT_PYRAMIDS = 10

FOCUS_SCOPES = {("GLB", 1): 0.35, ("USA", 1): 0.25, ("USA", 0): 0.20, ("EUR", 1): 0.10}


class PyramidPriority(Out):
    category_id: str
    category: str
    region: str
    delay: int
    multiplier: float
    submitted: int
    remaining: int
    priority_score: float


class Milestone(Out):
    date: str
    label: str
    source: Literal["platform_slide", "internal_planning", "quarter_calendar"]
    upcoming: bool


def in_q4(day: date) -> bool:
    return QUARTER_START <= day <= QUARTER_END


def rank_pyramids(
    grid: dict[str, Any], *, limit: int = 15, prioritize_theme: bool = True
) -> list[PyramidPriority]:
    if not grid:
        return []
    needed = int(grid["alphasPerPyramid"])
    if needed < 1:
        raise ValueError("alphasPerPyramid must be positive")
    names = {c["id"]: c.get("name") or c["id"] for c in grid["categories"]}
    rows: list[PyramidPriority] = []
    for cell in grid["cells"]:
        if not cell["synced"] or cell["multiplier"] is None:
            continue
        multiplier = float(cell["multiplier"])
        count = int(cell["alphaCount"])
        if not math.isfinite(multiplier) or count < 0 or count >= needed:
            continue
        region, delay, category = cell["region"], cell["delay"], cell["categoryId"]
        preference = FOCUS_SCOPES.get((region, delay), 0.0) if prioritize_theme else 0.0
        rows.append(
            PyramidPriority(
                category_id=category,
                category=names.get(category, category),
                region=region,
                delay=delay,
                multiplier=multiplier,
                submitted=count,
                remaining=needed - count,
                priority_score=round(multiplier + preference + 0.15 * count / needed, 4),
            )
        )
    rows.sort(
        key=lambda r: (-r.priority_score, r.remaining, r.region, r.delay, r.category, r.category_id)
    )
    return rows[: max(0, limit)]


def milestones(day: date) -> list[Milestone]:
    items = [
        Milestone(
            date="2026-11-09",
            label="Unused-field boost begins",
            source="platform_slide",
            upcoming=False,
        ),
        Milestone(
            date=POWER_POOL_DEADLINE.isoformat(),
            label="GLB D1 Power Pool theme ends",
            source="platform_slide",
            upcoming=False,
        ),
        Milestone(
            date="2026-11-30",
            label="November AQP review",
            source="internal_planning",
            upcoming=False,
        ),
        Milestone(
            date=QUARTER_END.isoformat(),
            label="Q4 ends",
            source="quarter_calendar",
            upcoming=False,
        ),
    ]
    for item in items:
        item.upcoming = item.date >= day.isoformat()
    return items
