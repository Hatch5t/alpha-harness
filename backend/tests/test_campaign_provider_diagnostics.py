"""Campaign provider schema stays compact; local Pydantic remains strict."""

import json
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError
from pydantic.alias_generators import to_camel

from alpha_harness.llm.keys import LLMError
from alpha_harness.llm.openai_compat import ProviderError
from alpha_harness.llm.service import LLMService, _provider_category
from alpha_harness.research.io import GENERATION_RESPONSE_SCHEMA
from alpha_harness.research.schemas import Generated, GeneratedAlpha, Hypothesis


class GenerationProviderSchemaTests(unittest.TestCase):
    def test_request_schema_uses_supported_simple_structure(self) -> None:
        encoded = json.dumps(GENERATION_RESPONSE_SCHEMA)
        for unsupported in ["$defs", "$ref", "minLength", "maxLength", "default", "title"]:
            self.assertNotIn(f'"{unsupported}"', encoded)
        self.assertEqual(
            set(GENERATION_RESPONSE_SCHEMA["properties"]),
            {to_camel(name) for name in Generated.model_fields},
        )
        for key, model in [("hypotheses", Hypothesis), ("alphas", GeneratedAlpha)]:
            item = GENERATION_RESPONSE_SCHEMA["properties"][key]["items"]
            self.assertEqual(
                set(item["properties"]), {to_camel(name) for name in model.model_fields}
            )
            self.assertEqual(
                set(item["required"]),
                {
                    to_camel(name)
                    for name, field in model.model_fields.items()
                    if field.is_required()
                },
            )

    def test_local_validation_still_rejects_invalid_alpha_references(self) -> None:
        valid_hypothesis = {
            "mechanism": "Analyst revisions may transmit price information with a delay.",
            "family": "analyst_revision",
            "direction": "positive",
            "horizon": "5 to 30 days",
            "assumptions": ["Point-in-time data"],
            "fieldsRequired": ["analyst_field"],
            "riskFactors": ["Industry effects"],
            "failureRegimes": ["Regime shifts"],
            "falsificationTests": ["Disappears after costs"],
            "independence": "Compare with standard momentum.",
        }
        with self.assertRaises(ValidationError):
            Generated.model_validate(
                {
                    "hypotheses": [valid_hypothesis],
                    "alphas": [{"expression": "rank(close)", "hypothesis": 4}],
                }
            )

    def test_error_category_never_contains_provider_response_text(self) -> None:
        self.assertEqual(
            _provider_category(ProviderError(400, "private prompt: invalid response_schema")),
            "schema_rejected",
        )
        self.assertEqual(
            _provider_category(ProviderError(400, "max_tokens not supported")),
            "output_limit_rejected",
        )
        self.assertEqual(_provider_category(ProviderError(403, "private key")), "access_refused")
        self.assertEqual(
            _provider_category(ProviderError(503, "server busy")), "provider_unavailable"
        )


class BoundedProviderAttemptTests(unittest.IsolatedAsyncioTestCase):
    async def check_rejection(self, status: int, detail: str, category: str) -> None:
        service: Any = object.__new__(LLMService)
        service._default_sampling = set()
        service.model_for = lambda _: SimpleNamespace(
            provider="google", id="gemini-3.5-flash-lite", ref="google:gemini-3.5-flash-lite"
        )
        service.keys = SimpleNamespace(
            choose=AsyncMock(return_value=SimpleNamespace(key_id=1)),
            secret=AsyncMock(return_value="fake-secret"),
            mark=AsyncMock(),
        )
        service.ledger = SimpleNamespace(penalise=AsyncMock(), record_tokens=AsyncMock())
        client = SimpleNamespace(generate=AsyncMock(side_effect=ProviderError(status, detail)))
        with (
            patch.object(LLMService, "_client", return_value=client),
            self.assertRaises(LLMError) as caught,
        ):
            await service.generate(
                system="test",
                user="test",
                model_ref="google:gemini-3.5-flash-lite",
                response_schema={"type": "object"},
                temperature=1.0,
                max_provider_attempts=1,
            )
        self.assertEqual(caught.exception.provider_category, category)
        self.assertEqual(caught.exception.provider_status, status)
        client.generate.assert_awaited_once()
        service.keys.choose.assert_awaited_once()

    async def test_capped_temperature_rejection_keeps_reason_and_does_not_retry(self) -> None:
        await self.check_rejection(400, "temperature unsupported", "temperature_rejected")

    async def test_capped_quota_rejection_keeps_reason_and_does_not_retry(self) -> None:
        await self.check_rejection(429, "requests per minute quota", "rate_limited")

    async def test_capped_key_refusal_keeps_reason_and_does_not_retry(self) -> None:
        await self.check_rejection(403, "permission denied", "access_refused")


if __name__ == "__main__":
    unittest.main()
