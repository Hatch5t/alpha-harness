"""Read-only Q4 campaign prioritisation.

This module makes suggestions, never claims that a pyramid or Alpha meets BRAIN's
submission checks. The BRAIN-provided counts and multipliers remain authoritative.
"""

from datetime import date
from typing import Any

QUARTER_START = date(2026, 10, 1)
QUARTER_END = date(2026, 12, 31)
EXPERT_SIGNALS = 20
EXPERT_PYRAMIDS = 10
EXPERT_PERFORMANCE = 0.5

#: Research preferences, not BRAIN rules or guaranteed rewards.
FOCUS_SCOPES: dict[tuple[str, int], float] = {
    ("GLB", 1): 0.35,
    ("USA", 1): 0.25,
    ("USA", 0): 0.20,
    ("EUR", 1): 0.10,
}


def in_q4(day: date) -> bool:
    """Whether the date falls in the announced Q4 2026 campaign."""
    return QUARTER_START <= day <= QUARTER_END


def rank_pyramids(
    grid: dict[str, Any], *, limit: int = 15
) -> list[dict[str, Any]]:
    """Prioritise feasible, incomplete, locally synced pyramids.

    A pyramid is considered formulated after the threshold supplied by the
    platform's existing pyramid endpoint (currently three submitted Alphas).
    We rank *research opportunities*, not expected PnL or payout.
    """
    needed = int(grid.get("alphasPerPyramid") or 3)
    if needed < 1:
        raise ValueError("alphasPerPyramid must be positive")
    names = {str(c["id"]): str(c.get("name") or c["id"]) for c in grid.get("categories", [])}
    rows: list[dict[str, Any]] = []
    for cell in grid.get("cells", []):
        if not cell.get("synced"):
            continue
        multiplier = cell.get("multiplier")
        if multiplier is None:
            continue
        region = str(cell.get("region") or "")
        delay = int(cell.get("delay", -1))
        category_id = str(cell.get("categoryId") or "")
        if not region or not category_id:
            continue
        alpha_count = max(0, int(cell.get("alphaCount") or 0))
        if alpha_count >= needed:
            continue
        # The scope preference and work-in-progress bonus are explicit and
        # modest, so an attractive multiplier is not mistaken for a signal.
        priority = (
            float(multiplier)
            + FOCUS_SCOPES.get((region, delay), 0.0)
            + 0.15 * alpha_count / needed
        )
        rows.append(
            {
                "categoryId": category_id,
                "category": names.get(category_id, category_id),
                "region": region,
                "delay": delay,
                "multiplier": float(multiplier),
                "submitted": alpha_count,
                "remaining": needed - alpha_count,
                "priorityScore": round(priority, 4),
                "isPowerPoolThemeScope": region == "GLB" and delay == 1,
            }
        )
    rows.sort(
        key=lambda r: (
            -r["priorityScore"],
            r["remaining"],
            r["region"],
            r["delay"],
            r["category"],
        )
    )
    return rows[: max(0, limit)]


def milestones(day: date) -> list[dict[str, str | bool]]:
    """Announced milestones plus internal review dates; never imply eligibility."""
    items = [
        ("2026-11-09", "Unused-field boost begins", "platform_slide"),
        ("2026-11-22", "GLB D1 Power Pool theme ends", "platform_slide"),
        ("2026-11-30", "November AQP monthly review", "internal_planning"),
        ("2026-12-31", "Q4 ends; December AQP review", "quarter_calendar"),
    ]
    return [
        {
            "date": when,
            "label": label,
            "source": source,
            "upcoming": when >= day.isoformat(),
        }
        for when, label, source in items
    ]
