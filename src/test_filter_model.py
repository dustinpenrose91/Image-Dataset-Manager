"""Tests for filter_model SQL builders — EAV-backed filter and sort fields."""
from __future__ import annotations

import unittest

from filter_model import (
    FilterRule, SortRule, build_filter_conditions, build_sort_clause,
)


class EavFieldTests(unittest.TestCase):

    def test_is_favorite_true(self):
        conds, params = build_filter_conditions(
            [FilterRule("is_favorite", "is_true", "")], labels=None, alias="a"
        )
        self.assertEqual(params, [])
        self.assertEqual(len(conds), 1)
        c = conds[0]
        self.assertIn("all_image_attributes", c)
        self.assertIn("key = 'is_favorite'", c)
        self.assertIn("a.asset_id", c)         # alias substituted
        self.assertTrue(c.endswith("= 1"))

    def test_is_favorite_false_uses_coalesce(self):
        conds, _ = build_filter_conditions(
            [FilterRule("is_favorite", "is_false", "")], labels=None, alias="a"
        )
        self.assertIn("COALESCE", conds[0])
        self.assertIn("!= 1", conds[0])

    def test_scan_at_compares_as_text(self):
        conds, params = build_filter_conditions(
            [FilterRule("scan_at", ">=", "2026-07-18 00:00:00")],
            labels=None, alias="a",
        )
        self.assertIn("key = 'scan_at'", conds[0])
        self.assertIn("a.asset_id", conds[0])
        self.assertTrue(conds[0].endswith(">= ?"))
        # Bound as text, not coerced to int like the integer dtype.
        self.assertEqual(params, ["2026-07-18 00:00:00"])

    def test_scan_at_sortable_expression_gets_alias(self):
        clause = build_sort_clause([SortRule("scan_at", desc=True)], alias="a")
        self.assertIn("key = 'scan_at'", clause)
        self.assertIn("a.asset_id", clause)
        self.assertIn("DESC", clause)
        self.assertTrue(clause.endswith("asset_id ASC"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
