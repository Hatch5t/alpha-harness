"""Real Vault reads preserve row alignment, including an empty daily query."""

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa

from alpha_harness.db.duck import Catalog
from alpha_harness.vault.store import AlphaVault, _by_alpha, _daily, _daily_traded


class DailyConversionTests(unittest.TestCase):
    def test_empty_arrow_tables_have_no_groups(self) -> None:
        table = pa.table(
            {
                "alpha_id": pa.array([], type=pa.string()),
                "date": pa.array([], type=pa.date32()),
                "pnl": pa.array([], type=pa.float64()),
                "turnover": pa.array([], type=pa.float64()),
            }
        )
        self.assertEqual(_daily(table), {})
        self.assertEqual(_daily_traded(table), {})

    def test_mismatched_columns_are_rejected_before_group_slicing(self) -> None:
        day = date(2024, 1, 1)
        for lengths in [(0, 1, 1), (1, 0, 1), (1, 1, 0), (2, 1, 1), (1, 2, 1), (1, 1, 2)]:
            with self.subTest(lengths=lengths), self.assertRaisesRegex(ValueError, "length"):
                _by_alpha(["a"] * lengths[0], [day] * lengths[1], [1.0] * lengths[2])

    def test_group_boundaries_preserve_each_date_and_value(self) -> None:
        first, second = date(2024, 1, 1), date(2024, 1, 3)
        self.assertEqual(
            _by_alpha(["a", "a", "b"], [first, second, second], [1.0, 3.0, 9.0]),
            {"a": {first: 1.0, second: 3.0}, "b": {second: 9.0}},
        )

    def test_duplicate_dates_or_unordered_groups_are_rejected(self) -> None:
        day = date(2024, 1, 1)
        for ids, days in [
            (["a", "a"], [day, day]),
            (["b", "a"], [day, day]),
            (["a", "a"], [day + timedelta(days=1), day]),
        ]:
            with self.subTest(ids=ids, days=days), self.assertRaisesRegex(ValueError, "sorted"):
                _by_alpha(ids, days, [1.0, 2.0])


class VaultSeriesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.catalog = Catalog(Path(folder.name) / "vault.duckdb")
        await self.catalog.open()
        self.addAsyncCleanup(self.catalog.close)
        self.vault = AlphaVault(self.catalog)

    async def test_missing_and_pnl_only_alphas_have_no_traded_series(self) -> None:
        await self.catalog.replace_pnl("quick", [("quick", date(2024, 1, 1), 12.0, None)])
        self.assertEqual(await self.vault.daily_pnl(["missing"]), {})
        self.assertEqual(await self.vault.series(["missing", "quick"]), {})
        self.assertEqual(await self.vault.series([]), {})

    async def test_only_finite_complete_pairs_are_used_on_their_original_dates(self) -> None:
        start = date(2024, 1, 1)
        values = [
            (10.0, 0.1),
            (None, 0.2),
            (30.0, None),
            (float("nan"), 0.4),
            (50.0, float("inf")),
            (float("inf"), 0.6),
            (70.0, float("nan")),
            (80.0, -0.1),
            (90.0, 0.9),
        ]
        await self.catalog.replace_pnl(
            "a", [("a", start + timedelta(days=i), p, t) for i, (p, t) in enumerate(values)]
        )
        self.assertEqual(
            await self.vault.series(["a"]),
            {"a": {start: (10.0, 0.1), start + timedelta(days=8): (90.0, 0.9)}},
        )
        # Reading for research never rewrites or drops the original incomplete observations.
        self.assertEqual(await self.catalog.scalar("SELECT count(*) FROM alpha_pnl"), 9)
