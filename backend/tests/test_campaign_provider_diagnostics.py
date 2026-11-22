"""Campaign provider schema stays compact; local Pydantic remains strict."""

import json
import unittest

from pydantic import ValidationError
from pydantic.alias_generators import to_camel

from alpha_harness.llm.openai_compat import ProviderError
from alpha_harness.llm.service import _provider_category
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
                {to_camel(name) for name, field in model.model_fields.items() if field.is_required()},
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
        self.assertEqual(_provider_category(ProviderError(503, "server busy")), "provider_unavailable")


if __name__ == "__main__":
    unittest.main()
