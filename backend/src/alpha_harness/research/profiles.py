"Source-backed, configurable opportunities; unknown rules are never guessed."

from typing import TYPE_CHECKING

from sqlalchemy import select

from ..catalog.queries import FieldFilter
from ..db.models import ResearchProfile, utcnow
from .schemas import Profile

if TYPE_CHECKING:
    from ..db.sqlite import Database


async def initialize(db: Database) -> None:
    definitions = [
        Profile(
            id="general",
            name="General Grandmaster Research",
            verified=True,
            source="Local conservative research policy; no competition eligibility claimed.",
            verified_on=utcnow().date(),
        ),
        *[
            Profile(
                id=key,
                name=name,
                supported=supported,
                constraints={
                    "status": "Rules, eligible data, rewards and deadlines require verification."
                },
                component_regions=regions,
                field_filter=FieldFilter(alpha_count_max=0) if key == "unused_fields" else None,
                max_operators=8 if key == "power_pool" else 32,
                max_fields=3 if key == "power_pool" else 12,
            )
            for key, name, supported, regions in [
                ("regular", "Regular BRAIN research", True, []),
                ("power_pool", "Power Pool research", True, []),
                ("unused_fields", "Unused data fields", True, []),
                ("aqp", "Alpha Quality Competition", True, []),
                ("arc", "ARC / multi-region components", True, ["USA", "EUR", "ASI", "GLB"]),
                ("orthogonal", "Pool-orthogonal research", True, []),
                ("python", "Python-alpha research", False, []),
            ]
        ],
    ]
    async with db.session() as session:
        for definition in definitions:
            if await session.get(ResearchProfile, definition.id) is None:
                session.add(
                    ResearchProfile(id=definition.id, document=definition.model_dump(mode="json"))
                )


async def listing(db: Database) -> list[Profile]:
    async with db.session() as session:
        return [
            Profile.model_validate(row.document)
            for row in await session.scalars(select(ResearchProfile).order_by(ResearchProfile.id))
        ]


def problems(profile: Profile, region: str, *, live: bool) -> list[str]:
    found: list[str] = []
    if not profile.supported or profile.id == "python":
        found.append("This profile requires an execution interface not available in this build.")
    if live and not profile.verified:
        found.append("Verify this profile's rules and source before live execution.")
    if profile.regions and region not in profile.regions:
        found.append("The chosen region is outside this profile's verified scope.")
    if profile.deadline and profile.deadline < utcnow().date():
        found.append("This opportunity's recorded deadline has passed.")
    if profile.verified_on and profile.verified_on > utcnow().date():
        found.append("A future verification date is not valid evidence.")
    return found
