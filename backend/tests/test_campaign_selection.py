"""Task selection preserves discovery isolation, audit history, and legacy databases."""

import asyncio
import json
import math
import sqlite3
import tempfile
import unittest
from collections import defaultdict
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx2
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from alpha_harness.api.deps import get_state
from alpha_harness.campaign_review import ExperimentPlan, review_candidates
from alpha_harness.db.models import CampaignExperiment, Study, StudyStatus, Trial, TrialState
from alpha_harness.db.sqlite import Database
from alpha_harness.main import create_app


class TaskSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "legacy.db"
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(
                "CREATE TABLE campaign_experiment "
                "(id INTEGER PRIMARY KEY, created_at DATETIME NOT NULL, plan JSON NOT NULL)"
            )
            conn.execute("INSERT INTO campaign_experiment VALUES (99, '2023-01-01', '{}')")
        self.db = Database(self.path)
        self.addAsyncCleanup(self.db.dispose)
        await self.db.create_all()
        await self.db.create_all()
        self.stored = {
            key: {
                "alpha_id": key,
                "region": "USA",
                "delay": 1,
                "expression": f"rank({key})",
                "status": "UNSUBMITTED",
                "checks": json.dumps([{"name": "LOW_SHARPE", "result": "PASS"}]),
            }
            for key in ["best", "weak", "copy", "bad", "baseline"]
        }
        self.stored["copy"]["expression"] = "rank( best );"
        self.stored["baseline"]["date_submitted"] = date(2022, 1, 1)
        self.stored["baseline"]["status"] = "ACTIVE"
        first = date(2022, 1, 1)
        self.series = {
            key: {
                first + timedelta(days=i): (
                    means[key] + (math.cos(i / 3) if key == "weak" else math.sin(i)),
                    0.01,
                )
                for i in range(700)
            }
            for key in self.stored
            for means in [{"best": 2, "weak": 1, "copy": 2, "bad": -2, "baseline": 1}]
        }
        locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.state = SimpleNamespace(
            db=self.db,
            optimizer=SimpleNamespace(lock=lambda task_id: locks[task_id]),
            alphas=SimpleNamespace(
                by_ids=AsyncMock(
                    side_effect=lambda ids: {
                        key: self.stored[key] for key in ids if key in self.stored
                    }
                ),
                series=AsyncMock(
                    side_effect=lambda ids: {
                        key: self.series[key] for key in ids if key in self.series
                    }
                ),
                submitted_members=AsyncMock(return_value=[self.stored["baseline"]]),
            ),
        )
        async with self.db.session() as session:
            task = Study(
                name="isolated-task",
                label="Earnings continuation",
                sampler="template",
                template_source="rank($field)",
                sampler_params={"region": "USA", "delay": 1},
                objectives=["train_sharpe"],
                max_trials=10,
                status=StudyStatus.COMPLETE,
            )
            session.add(task)
            await session.flush()
            self.task_id = task.id
            session.add_all(
                [
                    Trial(study_id=task.id, number=i, alpha_id=key, state=TrialState.COMPLETE)
                    for i, key in enumerate(["best", "weak", "copy", "bad", "best"])
                ]
            )
            session.add(Trial(study_id=task.id, number=5, state=TrialState.FAIL))
        self.body = {
            "taskId": self.task_id,
            "hypothesis": "Delayed earnings adjustment supports price continuation.",
            "baselineIds": ["baseline"],
            "region": "USA",
            "delay": 1,
            "purpose": "regular",
            "validationStart": "2023-06-01",
            "validationEnd": "2023-12-01",
            "costBps": 0,
            "shortlistSize": 2,
        }
        app = create_app()
        app.dependency_overrides[get_state] = lambda: self.state
        self.client = TestClient(app, base_url="http://localhost")
        self.addCleanup(self.client.close)
        self.headers = {"X-Harness-Client": "1"}

    def freeze(self) -> dict:
        response = self.client.post(
            "/api/campaign/experiments/from-task", json=self.body, headers=self.headers
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def test_selection_audit_retry_and_regular_evaluation(self) -> None:
        result = self.freeze()
        self.assertEqual(result["plan"]["candidateIds"], ["best", "weak"])
        self.assertEqual(result["source"]["totalTrials"], 6)
        self.assertEqual(result["source"]["uniqueAlphas"], 4)
        self.assertEqual(result["source"]["trialStates"], {"COMPLETE": 5, "FAIL": 1})
        self.assertEqual(result["source"]["selected"], 2)
        self.assertEqual(self.freeze()["id"], result["id"])
        url = f"/api/campaign/experiments/{result['id']}"
        page = self.client.get(f"{url}/selection?offset=1&limit=2")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(page.json()["total"], 4)
        self.assertEqual(len(page.json()["results"]), 2)
        all_rows = self.client.get(f"{url}/selection").json()["results"]
        copied = next(row for row in all_rows if row["alphaId"] == "copy")
        self.assertFalse(copied["selected"])
        self.assertIn("matches", copied["reason"])
        with patch("alpha_harness.api.campaign_research.local_power_pool") as pool:
            response = self.client.post(f"{url}/evaluate", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        pool.assert_not_called()
        for row in response.json()["candidates"]:
            self.assertFalse(
                any("AMER" in r or "theme" in r or "orthogonality" in r for r in row["reasons"])
            )

    async def test_validation_changes_cannot_change_selection(self) -> None:
        first = self.freeze()
        for key, days in self.series.items():
            for day in days:
                if day >= date(2023, 5, 27):
                    days[day] = (10000.0 if key == "weak" else -10000.0, 0.5)
        self.body["hypothesis"] += " Independent repeat for isolation testing."
        second = self.freeze()
        self.assertEqual(first["plan"]["candidateIds"], second["plan"]["candidateIds"])
        for result in [first, second]:
            page = self.client.get(f"/api/campaign/experiments/{result['id']}/selection").json()
            result["scores"] = [row["discoverySharpe"] for row in page["results"]]
        self.assertEqual(first["scores"], second["scores"])

    async def test_concurrent_retries_share_one_frozen_selection(self) -> None:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.client.app), base_url="http://localhost"
        ) as client:
            responses = await asyncio.gather(
                client.post(
                    "/api/campaign/experiments/from-task", json=self.body, headers=self.headers
                ),
                client.post(
                    "/api/campaign/experiments/from-task", json=self.body, headers=self.headers
                ),
            )
        self.assertTrue(all(response.status_code == 201 for response in responses))
        self.assertEqual(responses[0].json()["id"], responses[1].json()["id"])
        async with self.db.session() as session:
            total = await session.scalar(select(func.count()).select_from(CampaignExperiment))
        self.assertEqual(total, 2)

    async def test_missing_data_waits_and_unfinished_task_is_refused(self) -> None:
        self.series.pop("weak")
        response = self.client.post(
            "/api/campaign/experiments/from-task", json=self.body, headers=self.headers
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("weak", response.text)
        async with self.db.session() as session:
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(CampaignExperiment)), 1
            )
            task = await session.get(Study, self.task_id)
            task.status = StudyStatus.RUNNING
        response = self.client.post(
            "/api/campaign/experiments/from-task", json=self.body, headers=self.headers
        )
        self.assertEqual(response.status_code, 409, response.text)

    async def test_no_winners_still_produces_a_rejection_report(self) -> None:
        self.body["costBps"] = 100
        result = self.freeze()
        self.assertEqual(result["plan"]["candidateIds"], [])
        self.assertEqual(result["source"]["selected"], 0)
        self.assertTrue(result["source"]["rejections"])

    async def test_positive_and_negative_correlations_reduce_the_shortlist(self) -> None:
        for sign in [-1, 1]:
            self.series["weak"] = {
                day: (1 + sign * (pnl - 2), turnover)
                for day, (pnl, turnover) in self.series["best"].items()
            }
            self.body["hypothesis"] += f" Direction {sign}."
            result = self.freeze()
            self.assertEqual(result["plan"]["candidateIds"], ["best"])
            page = self.client.get(f"/api/campaign/experiments/{result['id']}/selection").json()
            weak = next(row for row in page["results"] if row["alphaId"] == "weak")
            self.assertAlmostEqual(weak["discoveryCorrelation"], sign)
            self.assertIn("correlation", weak["reason"])

    async def test_unmeasurable_pairs_do_not_read_as_diversified(self) -> None:
        self.series["weak"] = dict(list(self.series["weak"].items())[:100])
        result = self.freeze()
        self.assertEqual(result["plan"]["candidateIds"], ["best"])
        self.assertIn(
            "Insufficient common discovery observations for diversification",
            result["source"]["rejections"],
        )

    async def test_legacy_migration_and_audit_survive_task_deletion(self) -> None:
        result = self.freeze()
        async with self.db.session() as session:
            task = await session.get(Study, self.task_id)
            await session.delete(task)
        self.assertEqual(self.freeze()["id"], result["id"])
        page = self.client.get(f"/api/campaign/experiments/{result['id']}/selection").json()
        self.assertEqual(page["total"], 4)
        exported = self.client.get(f"/api/campaign/experiments/{result['id']}/export")
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertEqual(len(exported.json()["provenance"]["trials"]), 6)
        self.assertEqual(exported.json()["provenance"]["configuration"]["template"], "rank($field)")
        self.assertIn("attachment", exported.headers["content-disposition"])
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute("SELECT plan FROM campaign_experiment WHERE id=99").fetchone(), ("{}",)
            )
            indexes = conn.execute("PRAGMA index_list(campaign_experiment)").fetchall()
            self.assertTrue(
                any(row[1] == "uq_campaign_source_key" and row[2] == 1 for row in indexes)
            )

    async def test_legacy_plan_defaults_and_regular_market_checks(self) -> None:
        data = {
            key: value
            for key, value in self.body.items()
            if key not in {"taskId", "shortlistSize", "region", "delay", "purpose"}
        }
        data["candidateIds"] = ["best"]
        legacy = ExperimentPlan.model_validate(data)
        self.assertEqual((legacy.region, legacy.delay, legacy.purpose), ("GLB", 1, "power_pool"))
        regular = legacy.model_copy(update={"region": "USA", "purpose": "regular"})
        evidence = review_candidates(regular, self.stored, [], self.series, {})[0]
        self.assertFalse(
            any("outside" in r or "AMER" in r or "theme" in r for r in evidence.reasons)
        )
