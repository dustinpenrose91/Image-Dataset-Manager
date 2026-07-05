"""Tests for ui_dialogs.AddToDatasetDialog — sort modes, check-state
preservation across re-sorts, and settings persistence (sort mode + geometry).
"""
from __future__ import annotations

import os
import tempfile
import unittest

from PySide6.QtCore import QSettings, Qt

from qt_test_app import ensure_qapp
from ui_dialogs import AddToDatasetDialog

_app = None


def setUpModule():
    global _app
    _app = ensure_qapp()


DATASETS = [("beta", 5), ("alpha", 1), ("gamma", 9)]


class AddToDatasetDialogTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ini = os.path.join(self.tmp, "test_ui.ini")

    def _settings(self) -> QSettings:
        return QSettings(self.ini, QSettings.Format.IniFormat)

    def _names_in_order(self, dlg: AddToDatasetDialog) -> list[str]:
        return [
            dlg._list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(dlg._list.count())
        ]

    def _check(self, dlg: AddToDatasetDialog, name: str) -> None:
        for i in range(dlg._list.count()):
            item = dlg._list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == name:
                item.setCheckState(Qt.CheckState.Checked)
                return
        self.fail(f"{name!r} not in list")

    def test_default_sort_is_alphabetical(self):
        dlg = AddToDatasetDialog(DATASETS, settings=self._settings())
        self.assertEqual(self._names_in_order(dlg), ["alpha", "beta", "gamma"])

    def test_count_sort_descending_and_checks_survive_resort(self):
        dlg = AddToDatasetDialog(DATASETS, settings=self._settings())
        self._check(dlg, "beta")
        dlg._sort_combo.setCurrentIndex(dlg._sort_combo.findData("count"))
        self.assertEqual(self._names_in_order(dlg), ["gamma", "beta", "alpha"])
        self.assertEqual(dlg._checked_existing(), ["beta"])

    def test_dataset_names_returns_names_not_display_text(self):
        dlg = AddToDatasetDialog(DATASETS, settings=self._settings())
        self._check(dlg, "gamma")
        dlg._new_edit.setText("  fresh  ")
        self.assertEqual(dlg.dataset_names(), ["fresh", "gamma"])

    def test_sort_mode_and_geometry_persist_across_instances(self):
        dlg = AddToDatasetDialog(DATASETS, settings=self._settings())
        dlg._sort_combo.setCurrentIndex(dlg._sort_combo.findData("count"))
        dlg.resize(500, 640)
        dlg.done(0)  # reject also persists

        dlg2 = AddToDatasetDialog(DATASETS, settings=self._settings())
        self.assertEqual(dlg2._sort_combo.currentData(), "count")
        self.assertEqual(self._names_in_order(dlg2), ["gamma", "beta", "alpha"])
        self.assertEqual((dlg2.width(), dlg2.height()), (500, 640))

    def test_pinned_float_above_both_sort_modes(self):
        dlg = AddToDatasetDialog(
            DATASETS, settings=self._settings(), pinned={"beta"}
        )
        # alpha mode: beta pinned first, then a→g among the rest
        self.assertEqual(self._names_in_order(dlg), ["beta", "alpha", "gamma"])
        dlg._sort_combo.setCurrentIndex(dlg._sort_combo.findData("count"))
        # count mode: beta still first, rest by count desc
        self.assertEqual(self._names_in_order(dlg), ["beta", "gamma", "alpha"])
        # UserRole carries the bare name, not the glyphed display text
        self._check(dlg, "beta")
        self.assertEqual(dlg.dataset_names(), ["beta"])

    def test_no_existing_datasets_still_accepts_new_name(self):
        dlg = AddToDatasetDialog([], settings=self._settings())
        self.assertIsNone(dlg._list)
        dlg._new_edit.setText("first")
        self.assertEqual(dlg.dataset_names(), ["first"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
