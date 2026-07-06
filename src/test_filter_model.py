"""Tests for filter_model.build_filter_conditions — EAV boolean fields."""
from __future__ import annotations

import unittest

from filter_model import FilterRule, build_filter_conditions


class EavBooleanFilterTests(unittest.TestCase):

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

    def test_is_last_scan_field_exists(self):
        conds, _ = build_filter_conditions(
            [FilterRule("is_last_scan", "is_true", "")], labels=None, alias="a"
        )
        self.assertIn("key = 'is_last_scan'", conds[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
