"""Campaign integration with isolated state and mock BRAIN reads."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import numpy as np
from fastapi.testclient import TestClient

from alpha_harness.api.deps import get_state
from alpha_harness.brain.errors import BrainTransportError
from alpha_harness.campaign_review import ExperimentPlan, review_candidates
from alpha_harness.db.sqlite import Database
from alpha_harness.main import create_app
from alpha_harness.tools.power_pool import correlate, submission_order


def plan() -> ExperimentPlan:
    return ExperimentPlan(
        hypothesis="Earnings revisions should persist across reporting dates.",
        candidate_ids=["candidate"],
        baseline_ids=["baseline"],
        validation_start=date(2023, 6, 1),
        validation_end=date(2023, 12, 1),
        expected_theme="GLB Research Test Theme",
        cost_bps=0,
    )


def fake_series() -> dict[str, dict[date, tuple[float, float]]]:
    start = date(2023, 1, 1)
    return {
        "candidate": {start + timedelta(days=i): (2 + i % 7, 0.01) for i in range(335)},
        "baseline": {start + timedelta(days=i): (1 + (i % 11) * 2, 0.01) for i in range(335)},
    }


def candidate() -> dict[str, object]:
    return {
        "alpha_id": "candidate",
        "expression": "rank(close)",
        "region": "GLB",
        "delay": 1,
        "checks": json.dumps(
            [
                {"name": "LOW_SHARPE", "result": "PASS"},
                {"name": "LOW_GLB_AMER_SHARPE", "result": "PASS"},
                {"name": "POWER_POOL_CORRELATION", "result": "PASS"},
                {
                    "name": "MATCHES_THEMES",
                    "result": "PASS",
                    "themes": [{"name": "GLB Research Test Theme"}],
                },
            ]
        ),
    }


class CampaignEvidenceTests(unittest.TestCase):
    def test_duplicates_and_disjoint_window(self) -> None:
        series = fake_series()
        stored = {"candidate": candidate()}
        submitted = [{"alpha_id": "old", "expression": "rank( close ); // comment"}]
        pool = {"candidate": {"verdict": "clear"}}
        design = plan()
        first = review_candidates(design, stored, submitted, series, pool)[0]
        self.assertEqual(first.duplicate_ids, ["old"])
        self.assertEqual(first.amer_check, "PASS")
        self.assertEqual(first.theme_check, "PASS")
        self.assertIsNotNone(first.holdout_delta)
        self.assertEqual(first.holdout_days, first.portfolio_days)
        for day in series["candidate"]:
            if day >= design.validation_start:
                series["candidate"][day] = (-10.0, 0.01)
        second = review_candidates(design, stored, submitted, series, pool)[0]
        self.assertEqual(first.discovery_sharpe, second.discovery_sharpe)
        self.assertIsNone(second.holdout_sharpe)
        self.assertTrue(any("Validation Sharpe" in r for r in second.reasons))

    def test_portfolio_uses_only_common_finite_observations(self) -> None:
        series = fake_series()
        design = plan()
        series["baseline"] = {
            day: (pnl, turnover if day < design.validation_start else float("nan"))
            for day, (pnl, turnover) in series["baseline"].items()
        }
        result = review_candidates(design, {"candidate": candidate()}, [], series, {})[0]
        self.assertGreater(result.holdout_days, 60)
        self.assertEqual(result.portfolio_days, 0)
        self.assertIsNone(result.holdout_delta)

    def test_nearby_theme_is_not_accepted(self) -> None:
        design = plan().model_copy(update={"expected_theme": "GLB Research"})
        result = review_candidates(design, {"candidate": candidate()}, [], {}, {})[0]
        self.assertIsNone(result.theme_check)
        self.assertIsNone(result.holdout_delta)
        self.assertTrue(any("orthogonality" in r for r in result.reasons))

    def test_pool_with_present_but_unusable_series_is_unmeasured(self) -> None:
        start = date(2023, 1, 1)
        dates = [start + timedelta(days=i) for i in range(300)]
        a = np.sin(np.arange(300, dtype=float))
        missing = np.concatenate((np.full(295, np.nan), np.ones(5)))
        rows = correlate(
            (["a", "b", "c"], dates, np.column_stack((a, np.cos(np.arange(300)), missing))),
            {},
            ["a"],
            ["b", "c"],
        )
        self.assertEqual(rows[0]["verdict"], "unmeasured")
        picks, blocked, unmeasured = submission_order(
            (["a", "b"], dates, np.column_stack((a, np.cos(np.arange(300))))),
            {"a": {"region": "GLB"}, "b": {"region": "GLB"}},
            ["a"],
            {"GLB": ["b"]},
        )
        self.assertEqual([row["alphaId"] for row in picks], ["a"])
        self.assertEqual((blocked, unmeasured), ({}, []))


class CampaignApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        path = Path(self.scratch.name) / "test.db"
        with closing(sqlite3.connect(path)) as old, old:
            old.execute("CREATE TABLE old_example (data TEXT)")
            old.execute("INSERT INTO old_example VALUES ('keep')")
        self.db = Database(path)
        await self.db.create_all()
        await self.db.create_all()
        with closing(sqlite3.connect(path)) as old:
            self.assertEqual(old.execute("SELECT data FROM old_example").fetchone(), ("keep",))
        self.addAsyncCleanup(self.db.dispose)
        self.stored = {
            "candidate": candidate(),
            "baseline": {"alpha_id": "baseline", "date_submitted": date(2022, 1, 1)},
        }
        self.state = SimpleNamespace(
            db=self.db,
            alphas=SimpleNamespace(
                by_ids=AsyncMock(
                    side_effect=lambda ids: {a: self.stored[a] for a in ids if a in self.stored}
                ),
                submitted_members=AsyncMock(return_value=[]),
                series=AsyncMock(return_value=fake_series()),
                pnl_grid=AsyncMock(return_value=([], [], np.empty((0, 0)))),
            ),
            auth=SimpleNamespace(
                session=SimpleNamespace(authenticated=True, user_id="test-campaign")
            ),
            endpoints=SimpleNamespace(
                submission_activity=AsyncMock(
                    side_effect=BrainTransportError("offline", maybe_delivered=False)
                ),
                pyramid_alphas=AsyncMock(return_value=[]),
                pyramid_multipliers=AsyncMock(
                    side_effect=BrainTransportError("offline", maybe_delivered=False)
                ),
            ),
            queries=SimpleNamespace(synced_tuples=AsyncMock(return_value=[])),
            catalog=SimpleNamespace(query=AsyncMock(return_value=[])),
        )
        app = create_app()
        app.dependency_overrides[get_state] = lambda: self.state
        self.client = TestClient(app, base_url="http://localhost")
        self.addCleanup(self.client.close)
        self.headers = {"X-Harness-Client": "1"}

    async def test_campaign_partial_and_offline_status(self) -> None:
        with patch("alpha_harness.api.campaign.datetime") as clock:
            clock.now.return_value.date.return_value = date(2026, 11, 14)
            response = self.client.get("/api/campaign")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsNone(data["submittedSignals"])
        self.assertEqual(data["completedPyramids"], 0)
        self.assertIsNone(data["pyramidCandidates"])
        self.state.endpoints.pyramid_alphas.assert_awaited_once_with("2026-10-01", "2027-01-01")
        self.state.endpoints.pyramid_multipliers.return_value = []
        self.state.endpoints.pyramid_multipliers.side_effect = None
        with patch("alpha_harness.api.campaign.datetime") as clock:
            clock.now.return_value.date.return_value = date(2026, 11, 14)
            data = self.client.get("/api/campaign").json()
        self.assertEqual(data["pyramidCandidates"], [])

    async def test_no_silent_field_market_fallback(self) -> None:
        response = self.client.get(
            "/api/campaign/unused-fields?region=GLB&delay=1&universe=TOP3000"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"synced": False, "syncedAt": None, "total": 0, "results": []}
        )

    async def test_frozen_plan_review_and_manual_decision(self) -> None:
        payload = plan().model_dump(mode="json")
        created = self.client.post("/api/campaign/experiments", json=payload, headers=self.headers)
        self.assertEqual(created.status_code, 201, created.text)
        url = f"/api/campaign/experiments/{created.json()['id']}"
        with patch(
            "alpha_harness.api.campaign_research.local_power_pool",
            new=AsyncMock(return_value=SimpleNamespace(rows=[])),
        ) as pool:
            first = self.client.post(f"{url}/evaluate", headers=self.headers)
            retry = self.client.post(f"{url}/evaluate", headers=self.headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), retry.json())
        self.assertIsNotNone(first.json()["candidates"][0]["holdoutDelta"])
        pool.assert_awaited_once()
        decision = {
            "alphaId": "candidate",
            "decision": "approve_for_manual_review",
            "note": "Manually reviewed the evidence",
        }
        blocked = self.client.post(f"{url}/decision", json=decision, headers=self.headers)
        self.assertEqual(blocked.status_code, 422)
        decision["decision"] = "reject"
        saved = self.client.post(f"{url}/decision", json=decision, headers=self.headers)
        self.assertEqual(saved.status_code, 200, saved.text)
        duplicate = self.client.post(f"{url}/decision", json=decision, headers=self.headers)
        self.assertEqual(duplicate.status_code, 409)
        history = self.client.get("/api/campaign/experiments").json()
        self.assertEqual(history[0]["review"]["decision"]["decision"], "reject")

    async def test_prospective_plan_waits_for_validation_window(self) -> None:
        start = date.today() + timedelta(days=10)
        body = plan().model_copy(
            update={"validation_start": start, "validation_end": start + timedelta(days=100)}
        ).model_dump(mode="json")
        created = self.client.post("/api/campaign/experiments", json=body, headers=self.headers)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertTrue(created.json()["prospective"])
        url = f"/api/campaign/experiments/{created.json()['id']}/evaluate"
        result = self.client.post(url, headers=self.headers)
        self.assertEqual(result.status_code, 422, result.text)
        self.assertEqual(result.json()["detail"]["code"], "open_validation")

    async def test_clear_candidate_can_be_approved_locally(self) -> None:
        payload = plan().model_dump(mode="json")
        created = self.client.post("/api/campaign/experiments", json=payload, headers=self.headers)
        self.assertEqual(created.status_code, 201, created.text)
        url = f"/api/campaign/experiments/{created.json()['id']}"
        evidence = self.client.post(f"{url}/evaluate", headers=self.headers)
        self.assertEqual(evidence.status_code, 200, evidence.text)
        candidate_evidence = evidence.json()["candidates"][0]
        self.assertEqual(candidate_evidence["poolVerdict"], "clear")
        self.assertEqual(candidate_evidence["reasons"], [])
        response = self.client.post(
            f"{url}/decision",
            json={
                "alphaId": "candidate",
                "decision": "approve_for_manual_review",
                "note": "Reviewed the stored BRAIN checks and portfolio evidence.",
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["decision"]["decision"], "approve_for_manual_review")
