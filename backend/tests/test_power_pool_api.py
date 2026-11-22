"""The free preview and task creation retain the same hypothesis and request estimate."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_power_pool_runtime import OPERATORS, context

from alpha_harness.api import power_pool_lab
from alpha_harness.labs.launch import AddedTask
from alpha_harness.llm.registry import ModelInfo



class PowerPoolApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_and_created_task_forward_hypothesis_and_budget_estimate(self) -> None:
        model = ModelInfo(id="synthetic", provider="google", rpm=10, rpd=1, reset_timezone="UTC")
        state = SimpleNamespace(
            llm=SimpleNamespace(
                registry=SimpleNamespace(get=lambda _: model),
                runnable=AsyncMock(return_value=[{"ref": model.ref, "remainingToday": 1}]),
                generate=AsyncMock(side_effect=AssertionError("Preview must never call an LLM")),
            ),
            metadata=SimpleNamespace(cached_settings_schema=AsyncMock(return_value={})),
            catalog=None,
        )
        body = power_pool_lab.PowerPoolRequest(
            region="GLB", delay=1, universe="TOP3000", dataset_ids=["A", "B"],
            neutralizations=["INDUSTRY"], cores=1, simulations=10,
            hypothesis="Forecast revisions persist over time.",
            favour_operators=["ts_delta", "unavailable_operator"],
        )
        with (
            patch.object(
                power_pool_lab, "account_operators", new=AsyncMock(return_value=OPERATORS)
            ),
            patch.object(power_pool_lab, "legal_choices", return_value={
                "neutralization": {"choices": [{"value": "INDUSTRY"}]},
            }),
            patch.object(
                power_pool_lab, "synced_universes", new=AsyncMock(return_value=["TOP3000"])
            ),
            patch.object(
                power_pool_lab.power_pool, "context_for", new=AsyncMock(return_value=context("A"))
            ),
            patch.object(
                power_pool_lab, "add_study",
                new=AsyncMock(return_value=AddedTask(id=1, name="synthetic")),
            ) as add,
        ):
            preview = await power_pool_lab.preview(body, state)
            self.assertEqual(preview.problems, [])
            self.assertEqual(preview.llm_calls, 2)
            self.assertTrue(preview.warnings)
            self.assertIn(body.hypothesis, preview.prompt.user)
            self.assertIn("ts_delta", preview.prompt.user)
            self.assertNotIn("unavailable_operator", preview.prompt.user)
            await power_pool_lab.add_task(body, state)
        params = add.call_args.kwargs["params"]
        self.assertEqual(params.hypothesis, body.hypothesis)
        self.assertEqual(params.favour_operators, body.favour_operators)
        state.llm.generate.assert_not_awaited()
