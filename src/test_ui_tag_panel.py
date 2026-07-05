"""Tests for ui_tag_panel._TagTableModel incremental update behaviour."""
from __future__ import annotations

import unittest


from qt_test_app import ensure_qapp
from ui_tag_panel import _TagTableModel

_app = None


def setUpModule():
    global _app
    _app = ensure_qapp()


class TagTableModelTests(unittest.TestCase):

    def setUp(self):
        self.model = _TagTableModel()
        self.model.set_tags([("sky", "General", 3), ("sea", "General", 5)])
        self.reset_count = 0
        self.changed = []
        self.model.modelReset.connect(lambda: setattr(self, "reset_count", self.reset_count + 1))
        self.model.dataChanged.connect(lambda tl, br, roles=None: self.changed.append((tl.row(), tl.column())))

    def test_same_keys_patches_counts_without_reset(self):
        self.model.set_tags([("sky", "General", 9), ("sea", "General", 5)])
        self.assertEqual(self.reset_count, 0)
        self.assertIn((0, 2), self.changed)  # sky count cell changed
        self.assertEqual(self.model.index(0, 2).data(), 9)

    def test_no_change_emits_nothing(self):
        self.model.set_tags([("sky", "General", 3), ("sea", "General", 5)])
        self.assertEqual(self.reset_count, 0)
        self.assertEqual(self.changed, [])

    def test_key_set_change_resets(self):
        self.model.set_tags([("sky", "General", 3), ("grass", "General", 1)])
        self.assertEqual(self.reset_count, 1)
        self.assertEqual(self.model.rowCount(), 2)

    def test_tag_at_returns_name_and_type(self):
        self.assertEqual(self.model.tag_at(0), ("sky", "General"))
        self.assertIsNone(self.model.tag_at(99))


if __name__ == "__main__":
    unittest.main(verbosity=2)
