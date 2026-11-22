"""Real SQLite/controller/harvest with terminal fixtures; external calls forbidden."""

import asyncio
import json
import math
import tempfile
import unittest
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import func, select

from alpha_harness.api import research as api
from alpha_harness.api.deps import install_exception_handlers
from alpha_harness.brain.schemas import SimulationRequest, SimulationSettings
from alpha_harness.db.models import (
    ResearchBudget,
    ResearchCampaign,
    ResearchCandidate,
    ResearchProfile,
    ResearchReservation,
    SimStatus,
    SimulationRecord,
    Trial,
    TrialState,
    utcnow,
)
from alpha_harness.db.sqlite import Database
from alpha_harness.engine.slots import BatchEngine
from alpha_harness.engine.tracker import SimulationTracker
from alpha_harness.labs.template_document import TemplateBody
from alpha_harness.research import budget, evidence
from alpha_harness.research.schemas import CampaignSpec, Limits
from alpha_harness.research.service import ResearchError, ResearchService


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "research.db"
        self.db = Database(self.path)
        await self.db.create_all()
        self.addAsyncCleanup(self.db.dispose)
        self.forbidden = AsyncMock(side_effect=AssertionError("No external call authorized"))
        self.state = SimpleNamespace(
            db=self.db,
            engine=SimpleNamespace(enqueue=self.forbidden),
            alphas=SimpleNamespace(by_ids=self.forbidden),
            optimizer=None,
            llm=SimpleNamespace(generate=self.forbidden),
            models=SimpleNamespace(all=lambda: []),
            auth=SimpleNamespace(session=SimpleNamespace(authenticated=False)),
        )
        self.service = ResearchService(self.state)
        self.state.research = self.service
        await self.service.initialize()
        self.spec = CampaignSpec(
            hypothesis="Analyst revisions reflect delayed information diffusion.",
            validation_start=date(2025, 1, 1),
            validation_end=date(2025, 12, 31),
            baseline_ids=["synthetic-baseline"],
            region="GLB",
            delay=1,
        )

    async def create(self, **kwargs: Any) -> Any:
        ident = await self.service.create(self.spec.model_copy(update=kwargs))
        await self.service.control(ident, "start")
        return ident

    async def finish(self, ident: Any) -> Any:
        for _ in range(60):
            await self.service.advance(ident)
            row = await self.service.get(ident)
            if row.status in {"COMPLETE", "BLOCKED"}:
                return row
        raise AssertionError("Controller did not settle within bounded steps")

    async def until(self, ident: Any, stage: Any) -> Any:
        for _ in range(30):
            row = await self.service.get(ident)
            if row.cursor["stage"] == stage:
                return row
            self.assertEqual(row.status, "RUNNING", row.message)
            await self.service.advance(ident)
        raise AssertionError("Missing phase " + stage)

    async def test_complete_offline_pipeline_fair_allocation_and_private_report(self) -> None:
        ident = await self.create()
        row = await self.finish(ident)
        self.assertEqual(row.status, "COMPLETE", row.message)
        report = await self.service.report(ident, complete=True)
        json.dumps(report, allow_nan=False)
        self.assertEqual(report["budgets"]["quick"]["reserved"], 10)
        self.assertEqual(report["budgets"]["full"]["reserved"], 2)
        self.assertEqual(report["budgets"]["requests"]["reserved"], 2)
        allocation = Counter(c["provenance"]["dataset"] for c in report["candidates"] if c["quick"])
        self.assertEqual(sorted(allocation.values()), [5, 5])
        self.assertEqual(len(report["hypotheses"]), 6)
        self.assertEqual(report["counts"]["REJECTED"], 4)
        self.assertTrue(
            all(
                c["quick"]["settings"]["simulationMode"] == "QUICK"
                for c in report["candidates"]
                if c["quick"]
            )
        )
        self.assertTrue(
            all(
                c["full"]["settings"]["simulationMode"] == "FULL"
                for c in report["candidates"]
                if c["full"]
            )
        )
        for c in report["candidates"]:
            if c["evidence"]:
                self.assertFalse(c["evidence"]["submissionReady"])
                self.assertFalse(c["evidence"]["untouchedHoldout"])
                self.assertTrue(c["evidence"]["synthetic"])
                self.assertIn("multipleTesting", c["evidence"])
        async with self.db.session() as session:
            records = list(await session.scalars(select(SimulationRecord)))
            self.assertEqual(len(records), 12)
            self.assertTrue(
                all(r.status == SimStatus.COMPLETE and r.sent_at is None for r in records)
            )
            self.assertTrue(all(not r.allow_uncertain_retry for r in records))
        self.forbidden.assert_not_awaited()

    async def test_pause_reconnect_resume_preserves_reservations_and_lineage(self) -> None:
        ident = await self.create()
        await self.until(ident, "QUICK_WAIT")
        await self.service.control(ident, "pause")
        before = await budget.usage(self.db, ident, "offline")
        await self.db.dispose()
        self.db = Database(self.path)
        self.addAsyncCleanup(self.db.dispose)
        self.state.db = self.db
        self.service = ResearchService(self.state)
        for _ in range(3):
            await self.service.advance(ident)
        self.assertEqual((await self.service.get(ident)).status, "PAUSED")
        self.assertEqual(await budget.usage(self.db, ident, "offline"), before)
        await self.service.control(ident, "resume")
        self.assertEqual((await self.finish(ident)).status, "COMPLETE")
        report = await self.service.report(ident)
        self.assertEqual(report["budgets"]["quick"]["reserved"], 10)
        self.assertEqual(report["budgets"]["requests"]["reserved"], 2)

    async def test_crash_after_enqueue_recovers_terminal_record_without_second_send(self) -> None:
        ident = await self.create()
        await self.until(ident, "QUICK")
        engine = self.service._ios["offline"].engine
        original = engine.enqueue

        async def lost(*args: Any, **kwargs: Any) -> Any:
            await original(*args, **kwargs)
            raise RuntimeError("Lost after commit")

        with patch.object(engine, "enqueue", side_effect=lost):
            await self.service.advance(ident)
        self.assertEqual((await self.service.get(ident)).status, "BLOCKED")
        await self.service.control(ident, "resume")
        self.assertEqual((await self.finish(ident)).status, "COMPLETE")
        async with self.db.session() as session:
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(SimulationRecord)), 12
            )
        self.assertEqual((await budget.usage(self.db, ident, "offline"))["quick"], 10)

    async def test_crash_after_provider_reservation_never_replays_paid_operation(self) -> None:
        ident = await self.create()
        await self.until(ident, "GENERATE")
        io = self.service._ios["offline"]
        with (
            patch.object(io, "generate", side_effect=asyncio.CancelledError),
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.service.advance(ident)
        restarted = ResearchService(self.state)
        await restarted.advance(ident)
        self.assertEqual(len((await restarted.get(ident)).cursor["generated"]), 1)
        row = await self.finish(ident)
        self.assertEqual(row.status, "COMPLETE", row.message)
        report = await self.service.report(ident)
        self.assertEqual(report["budgets"]["requests"]["reserved"], 2)
        self.assertEqual(report["budgets"]["requests"]["unknownOperations"], 1)
        self.assertTrue(any(e["kind"] == "interrupted_generation" for e in report["events"]))

    async def test_zero_full_budget_saves_empty_approval_queue_with_reasons(self) -> None:
        ident = await self.create(limits=Limits(full=0))
        self.assertEqual((await self.finish(ident)).status, "COMPLETE")
        report = await self.service.report(ident)
        self.assertEqual(report["approvalQueue"], [])
        self.assertEqual(report["counts"]["QUICK_COMPLETE"], 10)
        self.assertTrue(all(c["reason"] for c in report["candidates"] if c["quick"]))
        with self.assertRaises(ResearchError):
            await self.service.decision(
                ident,
                report["candidates"][0]["id"],
                "approve_for_manual_review",
                "Must not approve Quick evidence.",
            )

    async def test_insufficient_token_budget_blocks_before_generation(self) -> None:
        ident = await self.create(limits=Limits(tokens=1))
        io = self.service._ios["offline"]
        with patch.object(io, "generate", wraps=io.generate) as generated:
            row = await self.finish(ident)
            self.assertEqual(row.status, "BLOCKED")
            self.assertIn("tokens budget exhausted", row.message)
            generated.assert_not_called()
        self.assertEqual(await budget.usage(self.db, ident, "offline"), {})

    async def test_cached_evidence_refresh_cannot_start_new_research_cycle(self) -> None:
        ident = await self.create(cycles=3, limits=Limits(quick=30, full=6, requests=6))
        await self.finish(ident)
        before = await budget.usage(self.db, ident, "offline")
        await self.service.refresh_evidence(ident)
        self.assertEqual((await self.finish(ident)).status, "COMPLETE")
        self.assertEqual(await budget.usage(self.db, ident, "offline"), before)
        report = await self.service.report(ident)
        self.assertGreater(sum(bool(c["evidence"]) for c in report["candidates"]), 0)
        self.assertTrue(
            all(c["evidence"]["reusedWindow"] for c in report["candidates"] if c["evidence"])
        )

    async def test_missing_evidence_does_not_become_negative_economic_memory(self) -> None:
        ident = await self.create()
        await self.finish(ident)
        async with self.db.session() as session:
            candidates = list(
                await session.scalars(
                    select(ResearchCandidate).where(
                        ResearchCandidate.campaign_id == ident,
                        ResearchCandidate.full_trial_id.is_not(None),
                    )
                )
            )
            for candidate in candidates:
                candidate.evidence = {
                    "recommendation": "needs_evidence_or_reject",
                    "failureClass": "insufficient_evidence",
                    "reasons": ["Missing recorded platform evidence."],
                }
            row = await session.get(ResearchCampaign, ident)
            row.status, row.cursor = "RUNNING", row.cursor | {"stage": "DECIDE"}
        await self.service.advance(ident)
        report = await self.service.report(ident)
        own = {c["hypothesisId"] for c in report["candidates"] if c["full"]}
        self.assertTrue(
            all(
                h["status"] == "EVIDENCE_INCOMPLETE" for h in report["hypotheses"] if h["id"] in own
            )
        )

    async def test_cancel_has_no_budget_reset_or_implicit_restart(self) -> None:
        ident = await self.create()
        await self.until(ident, "QUICK")
        await self.service.control(ident, "cancel")
        before = await budget.usage(self.db, ident, "offline")
        for _ in range(4):
            await self.service.advance(ident)
        self.assertEqual(await budget.usage(self.db, ident, "offline"), before)
        with self.assertRaises(ResearchError):
            await self.service.control(ident, "resume")

    async def test_api_explicit_acknowledgements_and_download(self) -> None:
        app = FastAPI()
        app.state.harness = self.state
        app.include_router(api.router)
        install_exception_handlers(app)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.put("/api/research/global-budget", json={"limits": {}})
            self.assertEqual(response.status_code, 422)
            live = await self.service.create(self.spec.model_copy(update={"mode": "live"}))
            response = await client.post(
                f"/api/research/{live}/authorize", json={"configHash": self.spec.digest()}
            )
            self.assertEqual(response.status_code, 422)
            response = await client.post(f"/api/research/{live}/control", json={"action": "start"})
            self.assertEqual(response.status_code, 422)
            ident = await self.create()
            await self.finish(ident)
            response = await client.get(f"/api/research/{ident}/download")
            self.assertEqual(response.status_code, 200)
            self.assertIn("no-store", response.headers["cache-control"])
            response = await client.post("/api/research/notebook-preview", json={"text": "invalid"})
            self.assertEqual(response.status_code, 422)

    async def test_global_budget_concurrent_last_slot_and_lifetime_day_reset(self) -> None:
        one = await self.create()
        two = await self.create()
        async with self.db.session() as session:
            policy = await session.get(ResearchBudget, "offline")
            policy.limits = Limits(requests=1, tokens=100000).model_dump(mode="json")
        outcomes = await asyncio.gather(
            budget.reserve(self.db, one, "a", {"requests": 1}),
            budget.reserve(self.db, two, "a", {"requests": 1}),
            return_exceptions=True,
        )
        self.assertEqual(sum(v is True for v in outcomes), 1)
        self.assertEqual(sum(isinstance(v, budget.BudgetBlocked) for v in outcomes), 1)
        winner = one if outcomes[0] is True else two
        self.assertFalse(await budget.reserve(self.db, winner, "a", {"requests": 1}))
        tomorrow = utcnow() + timedelta(days=1)
        with patch("alpha_harness.research.budget.utcnow", return_value=tomorrow):
            self.assertEqual(await budget.usage(self.db, None, "offline"), {})
            self.assertEqual((await budget.usage(self.db, winner, "offline"))["requests"], 1)

    async def test_global_batch_lease_blocks_other_campaign_until_settled(self) -> None:
        one = await self.create()
        two = await self.create()
        await budget.reserve(self.db, one, "quick:0", {"quick": 10})
        with self.assertRaises(budget.BudgetBlocked):
            await budget.reserve(self.db, two, "quick:0", {"quick": 10})
        await budget.observed(self.db, one, "quick:0", {})
        self.assertTrue(await budget.reserve(self.db, two, "quick:0", {"quick": 10}))
        for amount in [-1, 1.5, True]:
            with self.assertRaises(ValueError):
                await budget.reserve(self.db, two, "bad", {"tokens": amount})
        with self.assertRaises(ValueError):
            await budget.reserve(self.db, two, "bad", {"requests_per_minute": 1})

    async def test_actual_usage_exceeding_bound_stops_campaign_and_unknown_stays_unknown(
        self,
    ) -> None:
        ident = await self.create()
        await budget.reserve(self.db, ident, "request", {"requests": 1, "tokens": 5})
        await budget.observed(self.db, ident, "request", {"tokens": 6})
        self.assertEqual((await self.service.get(ident)).status, "BLOCKED")
        report = await self.service.report(ident)
        self.assertEqual(report["budgets"]["requests"]["unknownOperations"], 1)
        self.assertEqual(report["budgets"]["tokens"]["knownObserved"], 6)
        self.assertEqual((await budget.usage(self.db, ident, "offline"))["tokens"], 6)
        with self.assertRaises(ResearchError):
            await self.service.control(ident, "resume")

    async def test_paused_history_does_not_starve_new_campaign_worker(self) -> None:
        for _ in range(3):
            ident = await self.create()
            await self.service.control(ident, "pause")
        current = await self.create()
        await self.service.tick()
        await asyncio.gather(*self.service._jobs.values())
        self.assertEqual((await self.service.get(current)).cursor["stage"], "GENERATE")

    async def test_real_engine_dispatch_intent_survives_terminal_failure_and_never_sent_pause(
        self,
    ) -> None:
        endpoints = SimpleNamespace(create_simulation=self.forbidden)
        tracker = SimulationTracker(
            self.db,
            endpoints,
            on_change=AsyncMock(),
            on_unauthorized=AsyncMock(),
            on_alpha=AsyncMock(),
        )
        engine = BatchEngine(self.db, endpoints, tracker, on_unauthorized=AsyncMock())
        request = SimulationRequest(
            regular="rank(close)",
            settings=SimulationSettings(
                region="USA", delay=1, universe="TOP3000", simulation_mode="QUICK"
            ),
        )
        first = await engine.enqueue(
            [request], task="fixture", intent_keys=["stable"], allow_uncertain_retry=False
        )
        record_id = first["outcomes"][0]["recordId"]
        async with self.db.session() as session:
            record = await session.get(SimulationRecord, record_id)
            record.status = SimStatus.ERROR
        second = await engine.enqueue(
            [request], task="fixture", intent_keys=["stable"], allow_uncertain_retry=False
        )
        self.assertEqual(second["outcomes"][0]["recordId"], record_id)
        async with self.db.session() as session:
            record = await session.get(SimulationRecord, record_id)
            record.status = SimStatus.CANCELLED
        await engine.enqueue(
            [request], task="fixture", intent_keys=["stable"], allow_uncertain_retry=False
        )
        async with self.db.session() as session:
            record = await session.get(SimulationRecord, record_id)
            self.assertEqual(record.status, SimStatus.QUEUED)
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(SimulationRecord)), 1
            )
        with self.assertRaises(ValueError):
            await engine.enqueue(
                [request.model_copy(update={"regular": "rank(open)"})],
                task="fixture",
                intent_keys=["stable"],
            )
        self.forbidden.assert_not_awaited()

    async def test_restart_at_every_transition_retains_exact_caps(self) -> None:
        ident = await self.create()
        for _ in range(35):
            await self.service.advance(ident)
            row = await self.service.get(ident)
            if row.status == "COMPLETE":
                break
            self.assertEqual(row.status, "RUNNING", row.message)
            await self.db.dispose()
            self.db = Database(self.path)
            self.addAsyncCleanup(self.db.dispose)
            await self.db.create_all()
            self.state.db = self.db
            self.service = ResearchService(self.state)
        report = await self.service.report(ident)
        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(report["budgets"]["requests"]["reserved"], 2)
        self.assertEqual(report["budgets"]["quick"]["reserved"], 10)
        self.assertEqual(report["budgets"]["full"]["reserved"], 2)
        self.forbidden.assert_not_awaited()

    async def test_unknown_brain_send_holds_lease_until_explicit_settlement(self) -> None:
        ident = await self.create()
        row = await self.until(ident, "QUICK_WAIT")
        async with self.db.session() as session:
            trial = await session.get(Trial, row.cursor["quick_ids"][0])
            record = await session.get(SimulationRecord, trial.simulation_record_id)
            trial.state, trial.alpha_id, trial.message = TrialState.FAIL, None, "Transport ended."
            record.status, record.sent_at, record.alpha_id = SimStatus.ERROR, utcnow(), None
        await self.service.advance(ident)
        row = await self.service.get(ident)
        self.assertEqual(row.status, "BLOCKED")
        self.assertIn("Uncertain BRAIN", row.message)
        async with self.db.session() as session:
            lease = await session.scalar(
                select(ResearchReservation).where(
                    ResearchReservation.campaign_id == ident, ResearchReservation.meter == "quick"
                )
            )
            self.assertFalse(lease.settled)
        before = await budget.usage(self.db, ident, "offline")
        await self.service.resolve_uncertain(ident)
        self.assertEqual((await self.service.get(ident)).status, "CANCELLED")
        self.assertEqual(await budget.usage(self.db, ident, "offline"), before)

    async def test_persisted_rpm_survives_controller_restart(self) -> None:
        ident = await self.create(limits=Limits(requests_per_minute=1))
        await budget.reserve(self.db, ident, "first", {"requests": 1})
        with self.assertRaises(budget.BudgetBlocked) as caught:
            await budget.reserve(self.db, ident, "second", {"requests": 1})
        self.assertEqual(caught.exception.retry_after, 60)
        with patch(
            "alpha_harness.research.budget.utcnow", return_value=utcnow() + timedelta(seconds=61)
        ):
            self.assertTrue(await budget.reserve(self.db, ident, "second", {"requests": 1}))

    async def test_profile_rules_are_frozen_and_python_is_not_falsely_supported(self) -> None:
        ident = await self.create()
        row = await self.service.get(ident)
        config = CampaignSpec.model_validate(row.config)
        self.assertIsNotNone(config.profile_evidence)
        self.assertEqual(config.digest(), row.config_hash)
        async with self.db.session() as session:
            profile = await session.get(ResearchProfile, "general")
            profile.document = profile.document | {"maxOperators": 1}
        self.assertEqual(
            CampaignSpec.model_validate(
                (await self.service.get(ident)).config
            ).profile_evidence.max_operators,
            32,
        )
        unsupported = await self.create(profile_id="python")
        self.assertEqual((await self.service.get(unsupported)).status, "BLOCKED")

    async def test_component_report_refuses_missing_or_unreviewed_regions(self) -> None:
        ident = await self.create(profile_id="arc", component_group="synthetic-components")
        report = await self.service.report(ident)
        self.assertEqual(len(report["components"]["components"]), 4)
        self.assertFalse(report["components"]["allComponentsReviewed"])
        self.assertFalse(report["components"]["submissionReady"])

    async def test_template_campaign_uses_cartesian_grid_without_provider_requests(self) -> None:
        document = TemplateBody(
            name="Synthetic parameter grid",
            text="rank(ts_delta($field, $window))",
            variables={
                "field": {"kind": "fields"},
                "window": {"kind": "values", "values": "2, 4, 8"},
            },
        )
        ident = await self.create(templates=[document], limits=Limits(quick=6, full=0, requests=0))
        row = await self.finish(ident)
        self.assertEqual(row.status, "COMPLETE", row.message)
        report = await self.service.report(ident)
        self.assertNotIn("requests", report["budgets"])
        self.assertEqual(report["budgets"]["quick"]["reserved"], 6)
        self.forbidden.assert_not_awaited()

    async def test_cross_day_resume_charges_new_daily_budget_without_resetting_lifetime(
        self,
    ) -> None:
        one = await self.create()
        two = await self.create()
        async with self.db.session() as session:
            policy = await session.get(ResearchBudget, "offline")
            policy.limits = Limits(quick=10).model_dump(mode="json")
        await budget.reserve(self.db, one, "quick:0", {"quick": 10})
        await budget.observed(self.db, one, "quick:0", {})
        with patch(
            "alpha_harness.research.budget.utcnow", return_value=utcnow() + timedelta(days=1)
        ):
            await budget.reserve(self.db, two, "quick:0", {"quick": 10})
            await budget.observed(self.db, two, "quick:0", {})
            with self.assertRaises(budget.BudgetBlocked):
                await budget.reserve(self.db, one, "quick:0", {"quick": 10})
            async with self.db.session() as session:
                policy = await session.get(ResearchBudget, "offline")
                policy.limits = Limits(quick=20).model_dump(mode="json")
            self.assertFalse(await budget.reserve(self.db, one, "quick:0", {"quick": 10}))
            self.assertEqual((await budget.usage(self.db, None, "offline"))["quick"], 20)
            self.assertEqual((await budget.usage(self.db, one, "offline"))["quick"], 10)
        self.assertEqual((await budget.usage(self.db, None, "offline"))["quick"], 10)


class EvidenceTests(unittest.TestCase):
    def test_inconsistent_pairs_are_removed_by_date_never_positional_alignment(self) -> None:
        day = date(2024, 1, 1)
        dirty = {
            day: (1, 2),
            day + timedelta(days=1): (3,),
            day + timedelta(days=2): (float("nan"), 0),
            day + timedelta(days=3): (True, 1),
            day + timedelta(days=4): (2, 3),
            "bad": (4, 5),
        }
        self.assertEqual(
            evidence.usable(dirty), {day: (1.0, 2.0), day + timedelta(days=4): (2.0, 3.0)}
        )

    def test_correlation_requires_aligned_discovery_data_and_nonconstant_returns(self) -> None:
        spec = CampaignSpec(
            hypothesis="Synthetic economic mechanism.",
            validation_start="2025-01-01",
            validation_end="2025-12-31",
        )
        days = [date(2024, 1, 1) + timedelta(days=i) for i in range(90)]
        left = {d: (float(i), 0.0) for i, d in enumerate(days)}
        right = {d: (float(i) * 2, 0.0) for i, d in enumerate(days[30:])}
        value, count = evidence.correlation(left, right, spec)
        self.assertEqual(count, 60)
        self.assertAlmostEqual(value, 1.0)
        right = dict.fromkeys(days, (1.0, 0.0))
        self.assertIsNone(evidence.correlation(left, right, spec)[0])

    def test_covariance_regularizes_identical_series_and_never_zero_fills_missing_baseline(
        self,
    ) -> None:
        spec = CampaignSpec(
            hypothesis="Synthetic economic mechanism.",
            validation_start="2025-01-01",
            validation_end="2025-12-31",
        )
        series = {date(2025, 1, 1) + timedelta(days=i): (1000.0 + i, 0.0) for i in range(120)}
        report = evidence.covariance_estimate(["a", "b"], {"a": series, "b": series}, spec)
        self.assertTrue(report["available"])
        self.assertAlmostEqual(sum(report["weights"].values()), 1.0)
        self.assertFalse(
            evidence.covariance_estimate(["a", "missing"], {"a": series}, spec)["available"]
        )

    def test_supported_historical_full_evidence_is_reviewable_but_never_submission_ready(
        self,
    ) -> None:
        spec = CampaignSpec(
            hypothesis="Synthetic independent mechanism test only.",
            region="USA",
            validation_start=date(2025, 1, 1),
            validation_end=date(2025, 12, 31),
            baseline_ids=["baseline"],
        )
        start = date(2020, 1, 1)
        series = {
            "candidate": {
                start + timedelta(days=i): (100000 + 5000 * math.sin(i / 7), 0.01)
                for i in range(2200)
            },
            "baseline": {
                start + timedelta(days=i): (10000 + 5000 * math.cos(i / 11), 0.01)
                for i in range(2200)
            },
        }
        checks = json.dumps(
            [
                {"name": n, "result": "PASS"}
                for n in ["LOW_SHARPE", "CONCENTRATED_WEIGHT", "LOW_SUB_UNIVERSE_SHARPE"]
            ]
        )
        stored = {
            "candidate": {
                "alpha_id": "candidate",
                "expression": "rank(synthetic_field)",
                "region": "USA",
                "delay": 1,
                "simulation_mode": "FULL",
                "checks": checks,
            }
        }
        submitted = [
            {
                "alpha_id": "baseline",
                "expression": "rank(synthetic_baseline)",
                "date_submitted": "2019-01-01T00:00:00Z",
            }
        ]
        report = evidence.assess(
            spec,
            "candidate",
            stored,
            series,
            submitted,
            10,
            reused_window=False,
            created_before_validation=False,
            other_selected=[],
            sensitivity=[{"passed": True}, {"passed": True}],
            field_coverage={"synthetic_field": 1.0},
        )
        self.assertEqual(report["recommendation"], "approve_for_manual_review", report["reasons"])
        self.assertTrue(report["fullEligibilityObserved"])
        self.assertFalse(report["submissionReady"])
        self.assertFalse(report["untouchedHoldout"])
        self.assertTrue(report["multipleTesting"]["statisticallyAdjusted"])


class VectorOptionTests(unittest.TestCase):
    def test_explicit_vector_options_and_unknown_keywords(self) -> None:
        from alpha_harness.labs import search, template
        from alpha_harness.labs.fastexpr import operator_table, parse, validate

        operators = [{"name": "vec_percentage", "definition": "vec_percentage(x, percentage=0.5)"}]
        table = operator_table(operators)
        self.assertEqual(table["vec_percentage"].explicit, (("percentage", "0.5"),))
        self.assertTrue(validate(parse("vec_percentage(v)"), table, {"v"}))
        self.assertTrue(validate(parse("vec_percentage(v, precentage=0.5)"), table, {"v"}))
        self.assertFalse(validate(parse("vec_percentage(v, percentage=0.5)"), table, {"v"}))
        self.assertEqual(
            search.base("v", "vec_percentage", "percentage=0.5"),
            "ts_backfill(vec_percentage(v, percentage=0.5), 21)",
        )
        result = template.render(
            "rank($a)",
            {
                "variables": [
                    {
                        "name": "a",
                        "kind": "fields",
                        "vectorOptions": {"vec_percentage": [["percentage", "0.5"]]},
                    }
                ]
            },
            {"$a": "v", "$a#vector": "vec_percentage"},
        )
        self.assertEqual(result, "rank(vec_percentage(v, percentage=0.5))")

    def test_cached_baseline_identity_saves_rejection_without_invalid_plan(self) -> None:
        spec = CampaignSpec(
            hypothesis="Synthetic duplicate identity test.",
            validation_start=date(2025, 1, 1),
            validation_end=date(2025, 12, 31),
            baseline_ids=["same"],
        )
        result = evidence.assess(
            spec,
            "same",
            {},
            {},
            [],
            1,
            reused_window=False,
            created_before_validation=False,
            other_selected=[],
        )
        self.assertEqual(result["failureClass"], "duplicate")
        self.assertFalse(result["submissionReady"])
        self.assertIn("frozen submitted baseline", result["reasons"][0])

    def test_parameter_neighbors_keep_field_identity_and_small_changes(self) -> None:
        self.assertTrue(
            evidence.parameter_neighbor("rank(ts_delta(a, 10))", "rank(ts_delta(a, 12))")
        )
        self.assertFalse(
            evidence.parameter_neighbor("rank(ts_delta(a, 10))", "rank(ts_delta(b, 12))")
        )
        self.assertFalse(
            evidence.parameter_neighbor("rank(ts_delta(a, 10))", "rank(ts_delta(a, 200))")
        )
