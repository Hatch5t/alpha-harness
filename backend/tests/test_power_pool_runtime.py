"""Exercise the persisted Power Pool scheduler with local SQLite and fake providers only."""

import asyncio
import json
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from alpha_harness.db.models import Study, StudyStatus, Trial, TrialState
from alpha_harness.db.sqlite import Database
from alpha_harness.labs import power_pool, scheduler
from alpha_harness.labs.params import POWER_POOL_SAMPLER, PowerPoolParams, params_of
from alpha_harness.labs.power_pool import Context, Field
from alpha_harness.labs.power_pool_preflight import repair_temporal_vector, select_pending
from alpha_harness.labs.study import Optimizer
from alpha_harness.llm.budget import Headroom
from alpha_harness.llm.keys import BudgetExhaustedError
from alpha_harness.llm.registry import ModelInfo
from alpha_harness.tasks import cancel_background

OPERATORS = [
    {"name": "rank", "definition": "rank(x)"},
    {"name": "ts_delta", "definition": "ts_delta(x, d)"},
    {"name": "vec_avg", "definition": "vec_avg(x)"},
]


def context(dataset: str) -> Context:
    name = f"field_{dataset}"
    return Context(
        id=dataset, name=dataset, category="Synthetic", description="Tests only.",
        fields=(Field(name, "MATRIX", 1.0, "", None),), basics=(), groups=(),
        types={name: "MATRIX"}, own=frozenset({name}), names=frozenset({name}),
        held={name: frozenset({"TOP3000"})},
    )


class PowerPoolRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Database(Path(folder.name) / "harness.db")
        self.addAsyncCleanup(self.db.dispose)
        await self.db.create_all()
        self.engine = SimpleNamespace(
            enqueue=AsyncMock(side_effect=lambda requests, **_: {"outcomes": [{} for _ in requests]}),
            drop_queued=AsyncMock(return_value=0), set_quota=AsyncMock(), slots=8,
        )
        self.model = ModelInfo(id="synthetic", provider="google", rpm=10, rpd=10,
                               reset_timezone="UTC")
        self.llm = SimpleNamespace(
            registry=SimpleNamespace(get=lambda _: self.model),
            runnable=AsyncMock(return_value=[]), generate=AsyncMock(),
        )
        self.optimizer = self.new_optimizer()
        self.contexts = AsyncMock(side_effect=lambda *args: context(args[4]))
        self.patcher = patch.object(power_pool, "context_for", self.contexts)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        power_pool._calls.clear()
        power_pool._retry.clear()
        self.addAsyncCleanup(cancel_background)
        self.addCleanup(power_pool._calls.clear)
        self.addCleanup(power_pool._retry.clear)
        async with self.db.session() as session:
            row = Study(
                name="synthetic-runtime", sampler=POWER_POOL_SAMPLER,
                template_source="", sampler_params=PowerPoolParams(
                    region="GLB", delay=1, cores=1, universes=["TOP3000"],
                    neutralizations=["INDUSTRY"], dataset_ids=["A", "B"],
                    model=self.model.ref, only_model=True,
                    hypothesis="Forecast revisions persist over time.",
                ).dump(), objectives=["sharpe"], max_trials=20, batch_size=10,
                status=StudyStatus.RUNNING,
            )
            session.add(row)
            await session.flush()
            self.study_id = row.id

    def new_optimizer(self) -> Optimizer:
        optimizer = Optimizer(
            self.db, self.engine, SimpleNamespace(),
            SimpleNamespace(cached_operators=AsyncMock(return_value=OPERATORS)),
            on_change=AsyncMock(), alphas=SimpleNamespace(catalog=None),
        )
        optimizer.llm = self.llm
        return optimizer

    async def generate(self, **kwargs: object) -> SimpleNamespace:
        dataset = self.contexts.call_args.args[4]
        self.assertIn("Forecast revisions persist", kwargs["user"])
        return SimpleNamespace(
            text=json.dumps({"alphas": [
                {"expression": f"rank(ts_delta(field_{dataset}, {i + 1}))"} for i in range(20)
            ]}), total_tokens=10,
        )

    async def write_once(self) -> None:
        await scheduler.advance(self.optimizer, self.study_id)
        await power_pool._calls[self.study_id]

    async def rows(self) -> list[Trial]:
        async with self.db.session() as session:
            return list((await session.scalars(
                select(Trial).where(Trial.study_id == self.study_id).order_by(Trial.number)
            )).all())

    async def test_two_dataset_prefill_then_dispatch_does_not_generate_again(self) -> None:
        self.llm.generate.side_effect = self.generate
        await self.write_once()
        self.engine.enqueue.assert_not_awaited()
        await self.write_once()
        self.engine.enqueue.assert_not_awaited()
        self.assertEqual(await scheduler.advance(self.optimizer, self.study_id), 10)
        selected = [t for t in await self.rows() if t.state == TrialState.QUEUED]
        self.assertEqual(Counter(t.params["dataset"] for t in selected), {"A": 5, "B": 5})
        self.assertEqual(self.llm.generate.await_count, 2)
        self.assertEqual(len(await self.rows()), 40)

    async def test_fairness_uses_history_and_survives_optimizer_restart(self) -> None:
        self.llm.generate.side_effect = self.generate
        await self.write_once()
        await self.write_once()
        async with self.db.session() as session:
            ready = (await session.scalars(select(Trial).order_by(Trial.number).limit(10))).all()
            for row in ready:
                row.state = TrialState.COMPLETE
        self.optimizer = self.new_optimizer()
        self.assertEqual(await scheduler.advance(self.optimizer, self.study_id), 10)
        selected = [t for t in await self.rows() if t.state == TrialState.QUEUED]
        self.assertEqual(Counter(t.params["dataset"] for t in selected), {"B": 10})
        self.assertEqual(self.llm.generate.await_count, 2)

    async def test_budget_block_is_persisted_and_ready_work_is_not_stranded(self) -> None:
        self.llm.generate.side_effect = self.generate
        await self.write_once()
        error = BudgetExhaustedError(self.model, [Headroom(
            1, self.model.id, 10, 10, 0, 10, "requests_per_day", 3600,
        )])
        self.llm.generate.side_effect = error
        await self.write_once()
        row = await self.optimizer.get(self.study_id)
        self.assertGreater(params_of(row, PowerPoolParams).llm["blockedUntil"], time.time())
        power_pool._retry.clear()
        power_pool._calls.clear()
        self.optimizer = self.new_optimizer()
        self.assertEqual(await scheduler.advance(self.optimizer, self.study_id), 10)
        self.assertEqual(self.llm.generate.await_count, 2)
        self.assertTrue(all(t.params["dataset"] == "A" for t in await self.rows()))

    async def test_empty_budget_does_not_loop_requests_and_resumes_after_reset(self) -> None:
        self.llm.generate.side_effect = BudgetExhaustedError(self.model, [])
        await self.write_once()
        for _ in range(3):
            self.assertEqual(await scheduler.advance(self.optimizer, self.study_id), 0)
        self.assertEqual(self.llm.generate.await_count, 1)
        self.engine.enqueue.assert_not_awaited()
        async with self.db.session() as session:
            row = await session.get(Study, self.study_id)
            params = params_of(row, PowerPoolParams)
            params.llm["blockedUntil"] = time.time() - 1
            row.sampler_params = params.dump()
        power_pool._retry.clear()
        self.llm.generate.side_effect = self.generate
        await self.write_once()
        self.assertEqual(self.llm.generate.await_count, 2)

    async def test_pause_during_generation_keeps_output_parked_until_resume(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(**kwargs: object) -> SimpleNamespace:
            entered.set()
            await release.wait()
            return await self.generate(**kwargs)

        self.llm.generate.side_effect = delayed
        await scheduler.advance(self.optimizer, self.study_id)
        await asyncio.wait_for(entered.wait(), 5)
        with patch.object(scheduler, "start_waiting", new=AsyncMock()):
            await scheduler.pause(self.optimizer, self.study_id)
        release.set()
        await power_pool._calls[self.study_id]
        self.assertEqual(await scheduler.advance(self.optimizer, self.study_id), 0)
        self.engine.enqueue.assert_not_awaited()
        self.assertEqual(len(await self.rows()), 20)
        self.assertTrue(all(t.message == power_pool.PROPOSED for t in await self.rows()))
        await self.optimizer.set_status(self.study_id, StudyStatus.RUNNING)
        self.llm.generate.side_effect = self.generate
        await self.write_once()
        self.assertEqual(await scheduler.advance(self.optimizer, self.study_id), 10)

    async def test_legacy_tally_and_absent_new_fields_preserve_pending_work(self) -> None:
        self.llm.generate.side_effect = self.generate
        await self.write_once()
        async with self.db.session() as session:
            row = await session.get(Study, self.study_id)
            data = dict(row.sampler_params)
            data.pop("hypothesis", None)
            data.pop("favourOperators", None)
            data["llm"] = {"calls": 2}
            row.sampler_params = data
        self.assertEqual(await scheduler.advance(self.optimizer, self.study_id), 10)
        self.assertEqual(self.llm.generate.await_count, 1)

    async def test_insufficient_prompt_budget_pauses_without_provider_request(self) -> None:
        small = ModelInfo(id="small", provider="google", rpm=10, rpd=10,
                          reset_timezone="UTC", max_prompt_tokens=1)
        self.llm.registry.get = lambda _: small
        await self.write_once()
        self.llm.generate.assert_not_awaited()
        self.engine.enqueue.assert_not_awaited()
        row = await self.optimizer.get(self.study_id)
        self.assertEqual(row.status, StudyStatus.PAUSED)
        self.assertIn("prompt budget", row.message)

    def test_more_datasets_than_slots_do_not_starve_on_repeated_windows(self) -> None:
        ready = [SimpleNamespace(number=i, params={"dataset": f"D{i:02}"}, expression="rank(x)")
                 for i in range(13)]
        allocated = {}
        for _ in range(13):
            selected = select_pending(ready, 1, "", allocated)
            dataset = selected[0].params["dataset"]
            allocated[dataset] = allocated.get(dataset, 0) + 1
        self.assertEqual(set(allocated.values()), {1})
        self.assertEqual(len(allocated), 13)

    def test_ambiguous_vector_repairs_are_refused_without_formatting_changes(self) -> None:
        for text in [
            "vec_avg(ts_delta(v, other))", "vec_avg(ts_delta(v, 0))",
            "vec_avg(ts_delta(v, 2.5))", "vec_avg(ts_delta(v, 20, other))",
            "vec_avg(ts_delta(v, d=20))", "vec_avg(v + other)", " rank(vec_avg(v)) ",
        ]:
            with self.subTest(text=text):
                self.assertEqual(repair_temporal_vector(text, {"v": "VECTOR"}), text)
