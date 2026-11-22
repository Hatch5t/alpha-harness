"""Transactional campaign-lifetime and global-UTC-day reservations.

Reservations are conservative and never silently refunded. Observed usage is separate.
No network, model fallback or retry can occur inside this transaction.
"""

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, text, update

from ..db.models import (
    ResearchBudget,
    ResearchCampaign,
    ResearchDailyCharge,
    ResearchReservation,
    utcnow,
)
from .schemas import CampaignSpec, Limits

if TYPE_CHECKING:
    from ..db.sqlite import Database


class BudgetBlocked(RuntimeError):
    """A cap, pause or missing approval prevented a side effect."""

    retry_after: int | None = None


async def usage(db: Database, campaign_id: int | None, mode: str) -> dict[str, int]:
    if campaign_id is None:
        query = select(ResearchDailyCharge.meter, func.sum(ResearchDailyCharge.amount)).where(
            ResearchDailyCharge.mode == mode, ResearchDailyCharge.day == utcnow().date().isoformat()
        )
    else:
        query = select(
            ResearchReservation.meter,
            func.sum(
                func.max(ResearchReservation.amount, func.coalesce(ResearchReservation.actual, 0))
            ),
        ).where(ResearchReservation.mode == mode, ResearchReservation.campaign_id == campaign_id)
    meter_column = ResearchDailyCharge.meter if campaign_id is None else ResearchReservation.meter
    async with db.session() as session:
        return {
            str(k): int(v) for k, v in (await session.execute(query.group_by(meter_column))).all()
        }


async def reserve(db: Database, campaign_id: int, key: str, amounts: dict[str, int]) -> bool:
    "An atomic multi-meter reservation; False means the exact operation already exists."
    if not amounts or any(type(amount) is not int or amount < 0 for amount in amounts.values()):
        raise ValueError("Budget amounts cannot be negative.")
    if set(amounts) - {"quick", "full", "requests", "tokens", "microusd"}:
        raise ValueError("Unknown spending meter.")
    async with db.session() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        day = utcnow().date().isoformat()
        campaign = await session.get(ResearchCampaign, campaign_id)
        if campaign is None or campaign.status != "RUNNING":
            raise BudgetBlocked("Campaign is not running.")
        spec = CampaignSpec.model_validate(campaign.config)
        if campaign.mode == "live" and campaign.authorized_hash != spec.digest():
            raise BudgetBlocked("Live execution needs approval of this exact configuration.")
        previous = list(
            await session.scalars(
                select(ResearchReservation).where(
                    ResearchReservation.campaign_id == campaign_id, ResearchReservation.key == key
                )
            )
        )
        policy = await session.get(ResearchBudget, campaign.mode)
        if policy is None:
            raise BudgetBlocked("No global research budget is approved.")
        global_caps = Limits.model_validate(policy.limits).model_dump(by_alias=False)
        local_caps = spec.limits.model_dump(by_alias=False)
        if amounts.get("quick", 0) or amounts.get("full", 0):
            leases = set(
                await session.scalars(
                    select(ResearchReservation.campaign_id)
                    .where(
                        ResearchReservation.mode == campaign.mode,
                        ResearchReservation.meter.in_(["quick", "full"]),
                        ResearchReservation.settled.is_(False),
                    )
                    .distinct()
                )
            )
            if campaign_id not in leases and len(leases) >= global_caps["concurrent_batches"]:
                error = BudgetBlocked("Waiting fairly for the global research batch allocation.")
                error.retry_after = 3
                raise error
        if previous:
            if {p.meter: p.amount for p in previous} != amounts:
                raise BudgetBlocked("A reserved operation cannot change its approved cost.")
            if amounts.get("quick", 0) or amounts.get("full", 0):
                current_charges = set(
                    await session.scalars(
                        select(ResearchDailyCharge.reservation_id).where(
                            ResearchDailyCharge.reservation_id.in_([r.id for r in previous]),
                            ResearchDailyCharge.day == day,
                        )
                    )
                )
                for row in previous:
                    total = await session.scalar(
                        select(func.coalesce(func.sum(ResearchDailyCharge.amount), 0)).where(
                            ResearchDailyCharge.mode == campaign.mode,
                            ResearchDailyCharge.day == day,
                            ResearchDailyCharge.meter == row.meter,
                        )
                    )
                    if (
                        row.amount > global_caps[row.meter]
                        or int(total or 0) > global_caps[row.meter]
                    ):
                        raise BudgetBlocked(
                            "Current global limits block resuming this reserved batch."
                        )
                    if row.id in current_charges:
                        continue
                    spent = await session.scalar(
                        select(func.coalesce(func.sum(ResearchDailyCharge.amount), 0)).where(
                            ResearchDailyCharge.mode == campaign.mode,
                            ResearchDailyCharge.day == day,
                            ResearchDailyCharge.meter == row.meter,
                        )
                    )
                    if int(spent or 0) + row.amount > global_caps[row.meter]:
                        raise BudgetBlocked(
                            "Resuming this batch would exceed today's global budget."
                        )
                    session.add(
                        ResearchDailyCharge(
                            reservation_id=row.id,
                            mode=campaign.mode,
                            day=day,
                            meter=row.meter,
                            amount=row.amount,
                        )
                    )
                for row in previous:
                    row.settled = False
            return False
        if amounts.get("requests", 0):
            window = ResearchReservation.created_at >= utcnow() - timedelta(seconds=60)
            recent_global = await session.scalar(
                select(func.coalesce(func.sum(ResearchReservation.amount), 0)).where(
                    window,
                    ResearchReservation.mode == campaign.mode,
                    ResearchReservation.meter == "requests",
                )
            )
            recent_local = await session.scalar(
                select(func.coalesce(func.sum(ResearchReservation.amount), 0)).where(
                    window,
                    ResearchReservation.campaign_id == campaign_id,
                    ResearchReservation.meter == "requests",
                )
            )
            if (recent_global or 0) + amounts["requests"] > global_caps["requests_per_minute"] or (
                recent_local or 0
            ) + amounts["requests"] > local_caps["requests_per_minute"]:
                error = BudgetBlocked(
                    "Persisted research request throttle; waiting for the minute window."
                )
                error.retry_after = 60
                raise error
        local = {
            str(k): int(v)
            for k, v in (
                (
                    await session.execute(
                        select(
                            ResearchReservation.meter,
                            func.sum(
                                func.max(
                                    ResearchReservation.amount,
                                    func.coalesce(ResearchReservation.actual, 0),
                                )
                            ),
                        )
                        .where(ResearchReservation.campaign_id == campaign_id)
                        .group_by(ResearchReservation.meter)
                    )
                ).all()
            )
        }
        glob = {
            str(k): int(v)
            for k, v in (
                (
                    await session.execute(
                        select(ResearchDailyCharge.meter, func.sum(ResearchDailyCharge.amount))
                        .where(
                            ResearchDailyCharge.mode == campaign.mode,
                            ResearchDailyCharge.day == day,
                        )
                        .group_by(ResearchDailyCharge.meter)
                    )
                ).all()
            )
        }
        for meter, amount in amounts.items():
            if meter not in local_caps:
                raise ValueError("Unknown budget meter.")
            if int(local.get(meter, 0)) + amount > local_caps[meter]:
                raise BudgetBlocked(
                    f"Campaign {meter} budget exhausted; caps cannot expand automatically."
                )
            if int(glob.get(meter, 0)) + amount > global_caps[meter]:
                raise BudgetBlocked(
                    f"Global {meter} budget exhausted; UTC daily reset or approval is required."
                )
        for k, v in amounts.items():
            reservation = ResearchReservation(
                campaign_id=campaign_id, mode=campaign.mode, day=day, key=key, meter=k, amount=v
            )
            session.add(reservation)
            await session.flush()
            session.add(
                ResearchDailyCharge(
                    reservation_id=reservation.id, mode=campaign.mode, day=day, meter=k, amount=v
                )
            )
        return True


async def observed(db: Database, campaign_id: int, key: str, actual: dict[str, Any]) -> None:
    async with db.session() as session:
        rows = await session.scalars(
            select(ResearchReservation).where(
                ResearchReservation.campaign_id == campaign_id, ResearchReservation.key == key
            )
        )
        for row in rows:
            row.settled = True
            value = actual.get(row.meter)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                row.actual = value
                if value > row.amount:
                    await session.execute(
                        update(ResearchDailyCharge)
                        .where(
                            ResearchDailyCharge.reservation_id == row.id,
                            ResearchDailyCharge.amount < value,
                        )
                        .values(amount=value)
                    )
                    campaign = await session.get(ResearchCampaign, campaign_id)
                    if campaign is not None:
                        campaign.status = "BLOCKED"
                        campaign.cursor = campaign.cursor | {"usage_overrun": True}
                        campaign.message = (
                            "Provider usage exceeded its reserved bound; further calls stopped."
                        )
