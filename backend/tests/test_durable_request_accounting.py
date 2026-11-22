"""Fault injection at the actual service/key/SQLite boundary; no network clients."""

import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from alpha_harness.db.models import KeyUsage
from alpha_harness.db.sqlite import Database
from alpha_harness.llm.budget import ALL_MODELS, Ledger
from alpha_harness.llm.keys import BudgetExhaustedError, KeyStore, LLMError
from alpha_harness.llm.openai_compat import ProviderError
from alpha_harness.llm.registry import ModelInfo
from alpha_harness.llm.service import LLMService
from alpha_harness.sealing import Sealer


class DurableRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "state.db"
        self.db = Database(self.path)
        self.addAsyncCleanup(self.db.dispose)
        await self.db.create_all()
        self.sealer = Sealer(Path(folder.name) / "test.key")
        self.model = ModelInfo(
            id="synthetic", provider="google", rpm=100, rpd=2, reset_timezone="UTC"
        )
        self.provider = SimpleNamespace(
            generate=AsyncMock(return_value=("{}", {"total_tokens": 7}))
        )
        self.service = self.make_service()
        key = await self.service.keys.add("synthetic-test-key", daily_limit=2)
        self.key_id = key.id

    def make_service(self) -> LLMService:
        # Deliberately do not construct an HTTP transport, even for a fake provider.
        service = object.__new__(LLMService)
        service.db = self.db
        service.registry = SimpleNamespace(get=lambda _: self.model)
        service.ledger = Ledger(self.db)
        service.keys = KeyStore(self.db, self.sealer, service.ledger)
        service._default_sampling = set()
        service._client = lambda *_: self.provider
        return service

    async def call(self, **kwargs: object) -> object:
        return await self.service.generate(
            system="Synthetic fixture",
            user="Test",
            model_ref=self.model.ref,
            response_schema={},
            temperature=1.0,
            **kwargs,
        )

    async def usage(self) -> KeyUsage:
        async with self.db.session() as session:
            return await session.scalar(select(KeyUsage).where(KeyUsage.model == self.model.id))

    async def test_failure_and_success_each_count_once_before_dispatch(self) -> None:
        async def answer(**_: object) -> tuple[str, dict[str, int]]:
            # The budget must already be durable when the physical request starts.
            self.assertEqual((await self.usage()).requests, self.provider.generate.await_count)
            if self.provider.generate.await_count == 1:
                raise ProviderError(400, "invalid request")
            return "{}", {"total_tokens": 7}

        self.provider.generate.side_effect = answer
        with self.assertRaises(LLMError):
            await self.call(max_provider_attempts=1)
        await self.call(max_provider_attempts=1)
        row = await self.usage()
        self.assertEqual((row.requests, row.tokens, row.reported_requests), (2, 7, 1))
        with self.assertRaises(BudgetExhaustedError):
            await self.call(max_provider_attempts=1)
        self.assertEqual(self.provider.generate.await_count, 2)

    async def test_cancelled_request_retains_its_charge_after_database_restart(self) -> None:
        entered = asyncio.Event()

        async def interrupted(**_: object) -> tuple[str, dict[str, int]]:
            entered.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

        self.provider.generate.side_effect = interrupted
        task = asyncio.create_task(self.call(max_provider_attempts=1))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.db.dispose()
        self.db = Database(self.path)
        self.addAsyncCleanup(self.db.dispose)
        self.service = self.make_service()
        self.provider.generate.side_effect = None
        await self.call(max_provider_attempts=1)
        with self.assertRaises(BudgetExhaustedError):
            await self.call(max_provider_attempts=1)
        self.assertEqual((await self.usage()).requests, 2)

    async def test_503_retries_are_separately_reserved_and_stop_at_daily_cap(self) -> None:
        self.provider.generate.side_effect = ProviderError(503, "overloaded")
        with (
            patch("alpha_harness.llm.service.asyncio.sleep", new=AsyncMock()),
            self.assertRaises(BudgetExhaustedError),
        ):
            await self.call()
        self.assertEqual(self.provider.generate.await_count, 2)
        self.assertTrue(
            all(c.kwargs["single_attempt"] for c in self.provider.generate.call_args_list)
        )
        self.assertEqual((await self.usage()).requests, 2)

    async def test_temperature_retry_cannot_bypass_a_single_attempt_limit(self) -> None:
        self.provider.generate.side_effect = ProviderError(400, "temperature not supported")
        with self.assertRaises(LLMError):
            await self.call(max_provider_attempts=1)
        self.provider.generate.assert_awaited_once()
        self.assertEqual((await self.usage()).requests, 1)

    async def test_concurrent_requests_cannot_take_the_same_last_slot(self) -> None:
        result = await asyncio.gather(
            *(self.call(max_provider_attempts=1) for _ in range(4)), return_exceptions=True
        )
        self.assertEqual(sum(isinstance(r, BudgetExhaustedError) for r in result), 2)
        self.assertEqual(self.provider.generate.await_count, 2)
        self.assertEqual((await self.usage()).requests, 2)

    async def test_usage_is_attached_to_dispatch_day_across_midnight(self) -> None:
        with patch("alpha_harness.llm.budget.model_day", return_value="2026-11-21"):
            slot = await self.service.keys.choose(self.model, skip=set())
        with patch("alpha_harness.llm.budget.model_day", return_value="2026-11-22"):
            await self.service.ledger.record_tokens(slot, self.model, 9)
        async with self.db.session() as session:
            rows = list(await session.scalars(select(KeyUsage)))
        self.assertEqual({r.day for r in rows}, {"2026-11-21"})
        self.assertEqual({r.model for r in rows}, {self.model.id, ALL_MODELS})
        self.assertTrue(all((r.requests, r.tokens) == (1, 9) for r in rows))

    async def test_independent_services_share_atomic_database_ceiling(self) -> None:
        other = self.make_service()

        async def invoke(service: LLMService) -> object:
            return await service.generate(
                system="Synthetic",
                user="Test",
                model_ref=self.model.ref,
                response_schema={},
                temperature=1.0,
                max_provider_attempts=1,
            )

        results = await asyncio.gather(
            *(invoke(service) for service in [self.service, other, self.service, other]),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(r, BudgetExhaustedError) for r in results), 2)
        self.assertEqual(self.provider.generate.await_count, 2)
        self.assertEqual((await self.usage()).requests, 2)

    async def test_additive_migration_preserves_key_and_historical_usage(self) -> None:
        await self.call(max_provider_attempts=1)
        await self.db.dispose()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            key_before = connection.execute("SELECT key_sealed FROM api_key").fetchone()[0]
            connection.execute("ALTER TABLE key_usage DROP COLUMN reported_requests")
            connection.execute("ALTER TABLE simulation_record DROP COLUMN allow_uncertain_retry")
        self.db = Database(self.path)
        self.addAsyncCleanup(self.db.dispose)
        await self.db.create_all()
        await self.db.create_all()
        self.service = self.make_service()
        self.assertEqual(await self.service.keys.secret(self.key_id), "synthetic-test-key")
        row = await self.usage()
        self.assertEqual((row.requests, row.tokens, row.reported_requests), (1, 7, 0))
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(
                connection.execute("SELECT key_sealed FROM api_key").fetchone()[0], key_before
            )
            columns = {
                r[1]: r[4] for r in connection.execute("PRAGMA table_info(simulation_record)")
            }
            self.assertEqual(columns["allow_uncertain_retry"], "'1'")


if __name__ == "__main__":
    unittest.main()
