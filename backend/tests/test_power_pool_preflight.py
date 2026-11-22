"""Regress the first GLB D1 trial's structural failures without publishing its data."""

import unittest
from types import SimpleNamespace

from alpha_harness.labs.fastexpr import OperatorInfo
from alpha_harness.labs.params import PowerPoolParams
from alpha_harness.labs.power_pool import Context, Field, Rejected, check, user_prompt
from alpha_harness.labs.power_pool_preflight import (
    prefill_calls,
    repair_temporal_vector,
    select_pending,
)


class PowerPoolPreflightTests(unittest.TestCase):
    def test_repair_vector_inside_time_series_without_guessing_arithmetic(self) -> None:
        types = {"synthetic_vector": "VECTOR", "ordinary": "MATRIX"}
        bad = "rank(vec_avg(ts_delta(synthetic_vector, 20)))"
        fixed = "rank(ts_delta(vec_avg(synthetic_vector), 20))"
        self.assertEqual(repair_temporal_vector(bad, types), fixed)
        self.assertEqual(
            repair_temporal_vector("rank(vec_avg(ts_zscore(synthetic_vector, 20)))", types),
            "rank(ts_zscore(vec_avg(synthetic_vector), 20))",
        )
        self.assertEqual(
            repair_temporal_vector("rank(vec_avg(ts_delta(ordinary, 20)))", types),
            "rank(vec_avg(ts_delta(ordinary, 20)))",
        )
        self.assertEqual(
            repair_temporal_vector("rank(vec_avg(synthetic_vector + ordinary))", types),
            "rank(vec_avg(synthetic_vector + ordinary))",
        )

    def test_repaired_expression_passes_existing_brain_shape_checks(self) -> None:
        ctx = Context(
            id="synthetic",
            name="Synthetic",
            category="Analyst",
            description="Synthetic fields for tests only.",
            fields=(Field("synthetic_vector", "VECTOR", 1.0, "", None),),
            basics=(),
            groups=(),
            types={"synthetic_vector": "VECTOR"},
            own=frozenset({"synthetic_vector"}),
            names=frozenset({"synthetic_vector"}),
            held={"synthetic_vector": frozenset({"TOP3000"})},
        )
        table = {
            name: OperatorInfo(name, "", required, required, params)
            for name, required, params in [
                ("rank", 1, ("x",)),
                ("vec_avg", 1, ("x",)),
                ("ts_delta", 2, ("x", "d")),
            ]
        }
        original = "rank(vec_avg(ts_delta(synthetic_vector, 20)))"
        with self.assertRaises(Rejected):
            check(original, ctx, table)
        fixed, used = check(repair_temporal_vector(original, ctx.types), ctx, table)
        self.assertEqual(fixed, "rank(ts_delta(vec_avg(synthetic_vector), 20))")
        self.assertEqual(used, frozenset({"synthetic_vector"}))

    def test_user_research_hypothesis_is_in_the_actual_generation_prompt(self) -> None:
        ctx = Context(
            id="synthetic",
            name="Synthetic",
            category="Analyst",
            description="No private BRAIN metadata.",
            fields=(Field("synthetic_vector", "VECTOR", 1.0, "", None),),
            basics=(),
            groups=(),
            types={"synthetic_vector": "VECTOR"},
            own=frozenset({"synthetic_vector"}),
            names=frozenset({"synthetic_vector"}),
            held={"synthetic_vector": frozenset({"TOP3000"})},
        )
        run = PowerPoolParams(
            region="GLB",
            delay=1,
            universes=["TOP3000"],
            neutralizations=["INDUSTRY"],
            hypothesis="Test synthetic forecast revision acceleration, not static levels.",
        )
        operators = [
            {"name": "rank", "definition": "rank(x)", "category": "Cross Sectional"},
        ]
        prompt, count = user_prompt(ctx, operators, run, "None yet.", 0, 8000)
        self.assertIn("RESEARCH HYPOTHESIS", prompt)
        self.assertIn("Test synthetic forecast revision acceleration", prompt)
        self.assertEqual(count, 1)

    def test_prefill_is_small_but_prevents_single_dataset_monopoly(self) -> None:
        datasets = [f"dataset{i}" for i in range(13)]
        self.assertEqual(prefill_calls(10, datasets), 2)
        self.assertEqual(prefill_calls(10, ["one"]), 1)
        self.assertEqual(prefill_calls(500, datasets), 3)
        self.assertEqual(prefill_calls(0, datasets), 0)

    def test_replay_shape_balances_datasets_before_spending_simulations(self) -> None:
        # Synthetic reconstruction of the private experiment's structure:
        # 10 static estimates from dataset A; 20 time-aware ideas from dataset B.
        ready = [
            SimpleNamespace(
                number=i,
                params={"dataset": "A"},
                expression=f"rank(vec_avg(forecast_{i}))",
            )
            for i in range(10)
        ]
        ready.extend(
            SimpleNamespace(
                number=i + 10,
                params={"dataset": "B"},
                expression=(
                    f"rank(ts_delta(scalar_{i}, 20))"
                    if i % 2
                    else f"group_rank(ts_zscore(scalar_{i}, 20), industry)"
                ),
            )
            for i in range(20)
        )
        selection = select_pending(
            ready, 10, "Persistent analyst revisions predict future returns."
        )
        self.assertEqual(len(selection), 10)
        self.assertEqual(
            [sum(t.params["dataset"] == dataset for t in selection) for dataset in ("A", "B")],
            [5, 5],
        )
        self.assertEqual(len({t.number for t in selection}), 10)
        self.assertTrue(any("ts_delta" in t.expression for t in selection))
        self.assertTrue(any("ts_zscore" in t.expression for t in selection))

    def test_single_dataset_still_fills_small_batch(self) -> None:
        trials = [
            SimpleNamespace(number=i, params={"dataset": "one"}, expression=f"rank(field_{i})")
            for i in range(5)
        ]
        self.assertEqual(len(select_pending(trials, 5, "Test value ranking.")), 5)
        self.assertEqual(select_pending(trials, 0, ""), [])

    def test_ambiguous_and_formatting_only_changes_are_not_repairs(self) -> None:
        for expression in [
            " vec_avg(synthetic_vector) ",
            "vec_avg(ts_delta(synthetic_vector, ordinary))",
            "vec_avg(ts_delta(synthetic_vector, 0))",
            "vec_avg(ts_delta(synthetic_vector, 2.5))",
            "vec_avg(ts_delta(synthetic_vector, 20, ordinary))",
            "vec_avg(ts_delta(synthetic_vector, d=20))",
            "vec_avg(ts_corr(synthetic_vector, ordinary, 20))",
        ]:
            with self.subTest(expression=expression):
                self.assertEqual(
                    repair_temporal_vector(expression, {"synthetic_vector": "VECTOR"}), expression
                )

    def test_vector_preflight_rejects_raw_vectors_and_matrix_reduction(self) -> None:
        ctx = Context(
            id="synthetic",
            name="Synthetic",
            category="",
            description="",
            fields=(),
            basics=(),
            groups=(),
            types={"v": "VECTOR", "m": "MATRIX"},
            own=frozenset({"v", "m"}),
            names=frozenset({"v", "m"}),
            held={},
        )
        table = {
            name: OperatorInfo(name, "", count, count, params)
            for name, count, params in [
                ("rank", 1, ("x",)),
                ("vec_avg", 1, ("x",)),
                ("ts_delta", 2, ("x", "d")),
            ]
        }
        for text in [
            "rank(v)",
            "ts_delta(v, 20)",
            "vec_avg(v + m)",
            "vec_avg(ts_delta(m, 20))",
            "vec_avg(vec_avg(v))",
        ]:
            with self.subTest(text=text), self.assertRaises(Rejected):
                check(repair_temporal_vector(text, ctx.types), ctx, table)


if __name__ == "__main__":
    unittest.main()
