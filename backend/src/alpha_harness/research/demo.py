"Run the production controller with deterministic adapters in a disposable database."

import argparse
import asyncio
import json
import os
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..db.sqlite import Database
from .schemas import CampaignSpec
from .service import ResearchService


async def demonstrate() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="alpha-research-offline-") as folder:
        db = Database(Path(folder) / "state.db")
        await db.create_all()
        service = ResearchService(SimpleNamespace(db=db, engine=None, alphas=None, optimizer=None))
        try:
            ident = await service.create(
                CampaignSpec(
                    hypothesis=(
                        "Synthetic test: delayed information versus liquidity and "
                        "stability mechanisms."
                    ),
                    validation_start=date(2025, 1, 1),
                    validation_end=date(2025, 12, 31),
                    baseline_ids=["synthetic-baseline"],
                    region="GLB",
                    delay=1,
                )
            )
            await service.control(ident, "start")
            for _ in range(40):
                await service.advance(ident)
                row = await service.get(ident)
                if row.status == "COMPLETE":
                    report = await service.report(ident, complete=True)
                    candidate = report["candidates"][0]
                    await service.decision(
                        ident,
                        candidate["id"],
                        "reject",
                        "Synthetic engineering demonstration; no real research claim.",
                    )
                    return await service.report(ident, complete=True)
                if row.status == "BLOCKED":
                    raise RuntimeError(row.message)
            raise RuntimeError("Offline controller did not finish within its bounded transitions.")
        finally:
            await db.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = asyncio.run(demonstrate())
    raw = json.dumps(report, indent=2, allow_nan=False).encode()
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)


if __name__ == "__main__":
    main()
