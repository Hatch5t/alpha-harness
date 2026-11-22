"""Opt-in Quick-to-Full pilot: offline persistence and safety tests only."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from alpha_harness.db.models import Study, StudyStatus, Trial, TrialState
from alpha_harness.db.sqlite import Database
from alpha_harness.labs import power_pool, scheduler
from alpha_harness.labs.params import POWER_POOL_SAMPLER, PowerPoolParams, params_of
from alpha_harness.labs.pilot import MAX_FULL, MAX_LLM, MAX_QUICK, candidates, stage_promotions


def quick(number: int, dataset: str, sharpe: float, fitness: float, passed: bool = True) -> Trial:
    return Trial(
        study_id=1,
        number=number,
        params={"dataset": dataset},
        expression=f"rank(ts_delta(field_{dataset}_{number}, 20))",
        settings={"region": "GLB", "delay": 1, "universe": "TOP3000"},
        state=TrialState.COMPLETE,
        values=[sharpe],
        result={
            "quick": True,
            "stats": {"sharpe": sharpe, "fitness": fitness},
            "checks": [{"name": "LOW_SHARPE", "result": "PASS" if passed else "FAIL"}],
        },
    )


class PilotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "pilot.db"
        self.db = Database(self.path)
        self.addAsyncCleanup(self.db.dispose)
        await self.db.create_all()
        self.locks: dict[int, asyncio.Lock] = {}
        self.optimizer = SimpleNamespace(
            db=self.db,
            lock=lambda study_id: self.locks.setdefault(study_id, asyncio.Lock()),
        )
        async with self.db.session() as session:
            row = Study(
                name="controlled-test",
                sampler=POWER_POOL_SAMPLER,
                template_source="",
                sampler_params=PowerPoolParams(
                    region="GLB",
                    delay=1,
                    universes=["TOP3000"],
                    neutralizations=["INDUSTRY"],
                    dataset_ids=["A", "B"],
                    pilot=True,
                    pilot_llm_cap=MAX_LLM,
                    pilot_full_cap=MAX_FULL,
                    only_model=True,
                    cores=1,
                ).dump(),
                max_trials=MAX_QUICK,
                status=StudyStatus.RUNNING,
            )
            session.add(row)
            await session.flush()
            self.study_id = row.id
            await session.commit()

    async def put_quicks(self, items: list[Trial]) -> None:
        async with self.db.session() as session:
            for item in items:
                item.study_id = self.study_id
                session.add(item)
            await session.commit()

    async def trials(self) -> list[Trial]:
        async with self.db.session() as session:
            return list(
                (
                    await session.scalars(
                        select(Trial).where(Trial.study_id == self.study_id).order_by(Trial.number)
                    )
                ).all()
            )

    async def restart(self) -> None:
        await self.db.dispose()
        self.db = Database(self.path)
        self.addAsyncCleanup(self.db.dispose)
        self.locks = {}
        self.optimizer = SimpleNamespace(
            db=self.db,
            lock=lambda study_id: self.locks.setdefault(study_id, asyncio.Lock()),
        )

    async def test_promotion_uses_both_fitness_and_sharpe_and_diversifies(self) -> None:
        await self.put_quicks(
            [
                quick(0, "A", 1.5, 0.7),
                quick(1, "A", 1.7, 0.8),
                quick(2, "B", 1.3, 0.9),
                quick(3, "B", 1.9, 0.0),
                quick(4, "B", 0.8, 0.6, passed=False),
            ]
        )
        self.assertEqual(await stage_promotions(self.optimizer, self.study_id), 2)
        promoted = [t for t in await self.trials() if (t.params or {}).get("fullOf") is not None]
        self.assertEqual(len(promoted), 2)
        self.assertEqual({t.params["dataset"] for t in promoted}, {"A", "B"})
        self.assertEqual({t.params["fullOf"] for t in promoted}, {1, 2})
        self.assertTrue(all(t.settings["simulationMode"] == "FULL" for t in promoted))
        self.assertTrue(all(t.state == TrialState.PRUNED for t in promoted))
        self.assertEqual(await stage_promotions(self.optimizer, self.study_id), 0)
        await self.restart()
        self.assertEqual(await stage_promotions(self.optimizer, self.study_id), 0)
        self.assertEqual(
            len([t for t in await self.trials() if (t.params or {}).get("fullOf") is not None]),
            2,
        )

    async def test_no_passes_no_full_runs(self) -> None:
        await self.put_quicks([quick(0, "A", 0.9, 0.4, passed=False)])
        self.assertEqual(await stage_promotions(self.optimizer, self.study_id), 0)
        self.assertEqual(len(await self.trials()), 1)

    async def test_paused_or_stopped_pilot_never_stages(self) -> None:
        await self.put_quicks([quick(0, "A", 1.0, 0.7)])
        async with self.db.session() as session:
            row = await session.get(Study, self.study_id)
            params = params_of(row, PowerPoolParams)
            params.stopping = True
            row.sampler_params = params.dump()
        self.assertEqual(await stage_promotions(self.optimizer, self.study_id), 0)

    async def test_llm_requests_are_reserved_across_restart_and_pauses(self) -> None:
        accepted = await asyncio.gather(
            *[power_pool._reserve_pilot_request(self.optimizer, self.study_id) for _ in range(3)]
        )
        self.assertEqual(accepted, [True, True, False])
        async with self.db.session() as session:
            row = await session.get(Study, self.study_id)
            self.assertEqual(params_of(row, PowerPoolParams).llm["pilotRequestsStarted"], MAX_LLM)
            row.status = StudyStatus.PAUSED
        self.assertFalse(await power_pool._reserve_pilot_request(self.optimizer, self.study_id))
        await self.restart()
        async with self.db.session() as session:
            row = await session.get(Study, self.study_id)
            row.status = StudyStatus.RUNNING
        self.assertFalse(await power_pool._reserve_pilot_request(self.optimizer, self.study_id))

    async def test_resizing_cannot_expand_pilot_quick_or_core_budget(self) -> None:
        row = None
        async with self.db.session() as session:
            row = await session.get(Study, self.study_id)
        with self.assertRaises(ValueError):
            await scheduler.resize_task(self.optimizer, row, 2, 10)
        with self.assertRaises(ValueError):
            await scheduler.resize_task(self.optimizer, row, 1, 11)

    async def test_legacy_power_pool_tasks_still_default_to_unrestricted_mode(self) -> None:
        run = PowerPoolParams(
            region="GLB", delay=1, universes=["TOP3000"], neutralizations=["INDUSTRY"]
        )
        self.assertFalse(run.pilot)
        self.assertEqual(run.pilot_full_cap, 0)
        self.assertEqual(run.pilot_llm_cap, 0)

    async def test_near_duplicate_quick_expressions_only_promote_once(self) -> None:
        a = quick(0, "A", 1.4, 0.8)
        b = quick(1, "B", 1.3, 0.7)
        b.expression = "rank( ts_delta( field_A_0, 20 ) )"
        await self.put_quicks([a, b])
        self.assertEqual(len(candidates(await self.trials())), 1)
        self.assertEqual(await stage_promotions(self.optimizer, self.study_id), 1)


if __name__ == "__main__":
    unittest.main()
