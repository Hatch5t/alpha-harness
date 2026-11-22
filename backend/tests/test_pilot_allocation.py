"""A strict pilot cannot borrow or smuggle work into another task's batch."""

import unittest

from alpha_harness.engine.packer import BatchKey, WorkItem, allocate_slots, pack


class PilotAllocationTests(unittest.TestCase):
    def test_glb_pilot_has_one_batch_even_with_idle_core_lending(self) -> None:
        strict = frozenset({"pilot"})
        key = BatchKey("REGULAR", "EQUITY", "GLB", 1, "FASTEXPR", "QUICK")
        items = [WorkItem(i, key, "pilot", {}) for i in range(20)]
        allocation = allocate_slots(
            8,
            demand={"pilot": 4, "manual": 2},
            quotas={"pilot": key.cost},
            lend=True,
            strict_tasks=strict,
        )
        self.assertEqual(allocation["pilot"], 2)
        batches = pack(items, free_slots=8, task_capacity=allocation, strict_tasks=strict)
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0].items), 10)
        occupied = allocate_slots(
            6,
            demand={"pilot": 2},
            quotas={"pilot": 2},
            in_flight={"pilot": 2},
            lend=True,
            strict_tasks=strict,
        )
        self.assertEqual(occupied, {})

    def test_strict_task_never_rides_free_in_an_unrelated_batch(self) -> None:
        key = BatchKey("REGULAR", "EQUITY", "USA", 1, "FASTEXPR", "QUICK")
        items = [WorkItem(0, key, "manual", {}), WorkItem(1, key, "pilot", {})]
        strict = frozenset({"pilot"})
        batches = pack(
            items, free_slots=1, task_capacity={"manual": 1, "pilot": 0}, strict_tasks=strict
        )
        self.assertEqual([i.task for b in batches for i in b.items], ["manual"])


if __name__ == "__main__":
    unittest.main()
