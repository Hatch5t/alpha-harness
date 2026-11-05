"""Pure unit tests for the Q4 campaign prioritisation rules."""

import unittest
from datetime import date

from alpha_harness.campaign_rules import in_q4, milestones, rank_pyramids


class CampaignRulesTests(unittest.TestCase):
    def test_quarter_dates(self) -> None:
        self.assertFalse(in_q4(date(2026, 9, 30)))
        self.assertTrue(in_q4(date(2026, 10, 1)))
        self.assertTrue(in_q4(date(2026, 12, 31)))
        self.assertFalse(in_q4(date(2027, 1, 1)))

    def test_incomplete_synced_pyramids_only(self) -> None:
        grid = {
            "alphasPerPyramid": 3,
            "categories": [{"id": "fundamental", "name": "Fundamental"}],
            "cells": [
                {
                    "categoryId": "fundamental",
                    "region": "GLB",
                    "delay": 1,
                    "alphaCount": 2,
                    "synced": True,
                    "multiplier": 1.4,
                },
                {
                    "categoryId": "fundamental",
                    "region": "USA",
                    "delay": 1,
                    "alphaCount": 3,
                    "synced": True,
                    "multiplier": 1.8,
                },
                {
                    "categoryId": "fundamental",
                    "region": "EUR",
                    "delay": 1,
                    "alphaCount": 0,
                    "synced": False,
                    "multiplier": 1.9,
                },
            ],
        }
        ranked = rank_pyramids(grid)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["category"], "Fundamental")
        self.assertEqual(ranked[0]["remaining"], 1)
        self.assertTrue(ranked[0]["isPowerPoolThemeScope"])

    def test_no_data_and_invalid_threshold(self) -> None:
        self.assertEqual(rank_pyramids({}), [])
        with self.assertRaises(ValueError):
            rank_pyramids({"alphasPerPyramid": -1})

    def test_announced_deadline_and_reviews_distinguished(self) -> None:
        entries = milestones(date(2026, 11, 5))
        deadlines = {entry["date"]: entry for entry in entries}
        self.assertEqual(deadlines["2026-11-22"]["source"], "platform_slide")
        self.assertEqual(deadlines["2026-11-30"]["source"], "internal_planning")
        self.assertTrue(deadlines["2026-11-09"]["upcoming"])


if __name__ == "__main__":
    unittest.main()
