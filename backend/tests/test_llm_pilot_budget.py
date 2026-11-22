"""Provider-level pilot budget: no hidden retry after a reserved LLM attempt."""

import unittest
from unittest.mock import AsyncMock

from alpha_harness.llm.openai_compat import OpenAICompatible, ProviderError


class ProviderRequestBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_attempt_never_retries_503(self) -> None:
        client = object.__new__(OpenAICompatible)
        client._complete = AsyncMock(side_effect=ProviderError(503, "busy"))
        with self.assertRaises(ProviderError):
            await client.generate(
                model="synthetic",
                system="No proprietary prompts",
                user="test",
                temperature=1.0,
                schema=None,
                single_attempt=True,
            )
        client._complete.assert_awaited_once()

    async def test_successful_single_attempt_only_uses_one_request(self) -> None:
        client = object.__new__(OpenAICompatible)
        client._complete = AsyncMock(return_value=("{}", {"total_tokens": 2}))
        result = await client.generate(
            model="synthetic",
            system="test",
            user="test",
            temperature=None,
            schema=None,
            single_attempt=True,
        )
        self.assertEqual(result[0], "{}")
        client._complete.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
