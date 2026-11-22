"""Real scheduler, engine queue, harvest and SQLite; synthetic providers and results.

This demonstrates the bounded Phase 2 loop, not the future autonomous campaign system.
No BRAIN credentials, HTTP client, provider key or user data directory is read.
"""

import json
import unittest
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_power_pool_runtime as runtime
from sqlalchemy import select

from alpha_harness.db.models import SimStatus, SimulationRecord, Study, StudyStatus, utcnow
from alpha_harness.db.sqlite import Database
from alpha_harness.engine.lifecycle import new_record
from alpha_harness.engine.reconcile import reconcile_orphans
from alpha_harness.engine.slots import BatchEngine
from alpha_harness.engine.tracker import SimulationTracker
from alpha_harness.labs import power_pool, scheduler
from alpha_harness.labs.params import PowerPoolParams, params_of
from alpha_harness.labs.pilot import stage_promotions
from alpha_harness.labs.study import Optimizer


class PilotIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = runtime.PowerPoolRuntimeTests.asyncSetUp
    new_optimizer = runtime.PowerPoolRuntimeTests.new_optimizer
    generate = runtime.PowerPoolRuntimeTests.generate
    rows = runtime.PowerPoolRuntimeTests.rows
    write_once = runtime.PowerPoolRuntimeTests.write_once

    async def configure(self) -> None:
        self.saved = {}
        self.endpoints = SimpleNamespace(
            create_simulation=AsyncMock(side_effect=self.accept),
            list_alphas=AsyncMock(return_value={"results": []}),
        )
        self.attach()
        async with self.db.session() as session:
            row = await session.get(Study, self.study_id)
            params = params_of(row, PowerPoolParams)
            params.pilot = True
            params.pilot_llm_cap = params.pilot_full_cap = 2
            row.sampler_params = params.dump()
            row.max_trials = 10
            row.task = "pilot-fixture"
        await self.engine.set_quota("pilot-fixture", 1)
        self.llm.generate.side_effect = self.generate

    def attach(self) -> None:
        tracker = SimulationTracker(
            self.db,
            self.endpoints,
            on_change=AsyncMock(),
            on_unauthorized=AsyncMock(return_value=False),
            on_alpha=AsyncMock(),
        )
        self.engine = BatchEngine(
            self.db, self.endpoints, tracker, on_unauthorized=AsyncMock(return_value=False)
        )
        self.engine.quick_allowed = True
        self.engine.lend_idle_cores = True
        # Empty global Quick regions is intentional: the pilot must explicitly request Quick.
        self.optimizer = Optimizer(
            self.db,
            self.engine,
            self.endpoints,
            SimpleNamespace(cached_operators=AsyncMock(return_value=runtime.OPERATORS)),
            on_change=AsyncMock(),
            alphas=SimpleNamespace(
                catalog=None,
                by_ids=AsyncMock(side_effect=lambda ids: {i: self.saved[i] for i in ids}),
                with_families=AsyncMock(side_effect=lambda saved: saved),
            ),
        )
        self.optimizer.llm = self.llm

    async def accept(self, requests: object) -> SimpleNamespace:
        del requests
        return SimpleNamespace(
            location=f"/simulations/s{self.endpoints.create_simulation.await_count}",
            rate_limit=None,
            retry_after=None,
            status=201,
        )

    async def finish_dispatched(self) -> None:
        async with self.db.session() as session:
            records = list(
                await session.scalars(
                    select(SimulationRecord).where(SimulationRecord.status == SimStatus.RUNNING)
                )
            )
            for record in records:
                record.status = SimStatus.COMPLETE
                record.finished_at = utcnow()
                if record.is_batch:
                    record.children_expanded = True
                    continue
                alpha_id = f"fixture_{record.id}"
                record.alpha_id = alpha_id
                self.saved[alpha_id] = {
                    "alpha_id": alpha_id,
                    "sharpe": 1.4,
                    "fitness": 0.8,
                    "simulation_mode": record.payload["settings"]["simulationMode"],
                    "checks": json.dumps([{"name": "LOW_SHARPE", "result": "PASS"}]),
                }

    async def test_bounded_quick_full_loop_survives_pause_restart_and_repeated_ticks(self) -> None:
        await self.configure()
        await self.write_once()
        await self.write_once()
        self.assertEqual(await scheduler.advance(self.optimizer, self.study_id), 10)
        quicks = [t for t in await self.rows() if t.state == "QUEUED"]
        self.assertEqual(Counter(t.params["dataset"] for t in quicks), {"A": 5, "B": 5})
        self.assertTrue(all(t.settings["simulationMode"] == "QUICK" for t in quicks))
        self.assertEqual(await self.engine._fill_slots(), 1)
        self.assertEqual(await self.engine._fill_slots(), 0)
        await self.finish_dispatched()
        await self.optimizer.advance(self.study_id)
        fulls = [t for t in await self.rows() if t.params.get("fullOf") is not None]
        self.assertEqual(len(fulls), 2)
        promoted_from = {t.params["fullOf"] for t in fulls}
        self.assertTrue(all(t.params["reason"] for t in fulls))
        self.assertEqual({t.params["dataset"] for t in fulls}, {"A", "B"})

        # Pause after reservation but before enqueue, then reconnect the actual database.
        with patch.object(scheduler, "start_waiting", new=AsyncMock()):
            await scheduler.pause(self.optimizer, self.study_id)
        await self.db.dispose()
        self.db = Database(self.db_path)
        self.addAsyncCleanup(self.db.dispose)
        power_pool._calls.clear()
        power_pool._retry.clear()
        self.attach()
        self.assertEqual(await scheduler.advance(self.optimizer, self.study_id), 0)
        await self.optimizer.set_status(self.study_id, StudyStatus.RUNNING)
        await self.engine.set_quota("pilot-fixture", 1)
        self.assertEqual(await scheduler.advance(self.optimizer, self.study_id), 2)
        self.assertEqual(await self.engine._fill_slots(), 1)
        await self.finish_dispatched()
        await self.optimizer.advance(self.study_id)
        for _ in range(3):
            self.assertEqual((await self.optimizer.advance(self.study_id))["asked"], 0)
        rows = await self.rows()
        self.assertEqual(sum(t.state == "COMPLETE" for t in rows), 12)
        self.assertEqual((await self.optimizer.get(self.study_id)).status, StudyStatus.COMPLETE)
        self.assertEqual(self.llm.generate.await_count, 2)
        physical = self.endpoints.create_simulation.call_args_list
        self.assertEqual([len(call.args[0]) for call in physical], [10, 2])
        self.assertEqual(
            {t.params["fullOf"] for t in rows if "fullOf" in t.params},
            promoted_from,
        )
        exported = json.loads(
            json.dumps([{"number": t.number, "params": t.params, "result": t.result} for t in rows])
        )
        self.assertEqual(len(exported), 42)

    async def test_uncertain_pilot_send_is_recovered_or_stopped_without_respending(self) -> None:
        await self.configure()
        from alpha_harness.brain.schemas import SimulationRequest, SimulationSettings

        request = SimulationRequest(
            regular="rank(synthetic_field)",
            settings=SimulationSettings(
                region="GLB", delay=1, universe="TOP3000", simulationMode="QUICK"
            ),
        )
        record = new_record(
            request,
            task="pilot-fixture",
            status=SimStatus.ORPHANED,
            allow_uncertain_retry=True,
            sent_at=utcnow() - timedelta(hours=1),
        )
        async with self.db.session() as session:
            session.add(record)
        await self.db.dispose()
        self.db = Database(self.db_path)
        self.addAsyncCleanup(self.db.dispose)
        # An existing PR #4 task must inherit the cap when its schema is upgraded.
        await self.db.create_all()
        result = await reconcile_orphans(self.db, self.endpoints, on_alpha=lambda _: None)
        self.assertEqual(result["requeued"], 0)
        async with self.db.session() as session:
            stored = await session.get(SimulationRecord, record.id)
            self.assertEqual(stored.status, SimStatus.ERROR)
            self.assertFalse(stored.allow_uncertain_retry)
            self.assertIn("Hard budget", stored.message)
        self.endpoints.create_simulation.assert_not_awaited()

    async def test_partial_engine_outcomes_leave_trials_parked(self) -> None:
        await self.configure()
        await self.write_once()
        await self.write_once()
        with (
            patch.object(self.engine, "enqueue", new=AsyncMock(return_value={"outcomes": []})),
            self.assertRaisesRegex(ValueError, "outcomes"),
        ):
            await scheduler.advance(self.optimizer, self.study_id)
        self.assertTrue(all(t.state == "PRUNED" for t in await self.rows()))
        self.endpoints.create_simulation.assert_not_awaited()

    async def test_paused_or_inflight_task_cannot_stage_full_promotions(self) -> None:
        await self.configure()
        from test_power_pool_pilot import quick

        async with self.db.session() as session:
            first, pending = quick(0, "A", 1.4, 0.8), quick(1, "B", 1.4, 0.8)
            pending.state = "RUNNING"
            session.add_all([first, pending])
        self.assertEqual(await stage_promotions(self.optimizer, self.study_id), 0)
        async with self.db.session() as session:
            row = await session.get(Study, self.study_id)
            row.status = StudyStatus.PAUSED
        self.assertEqual(await stage_promotions(self.optimizer, self.study_id), 0)


if __name__ == "__main__":
    unittest.main()
