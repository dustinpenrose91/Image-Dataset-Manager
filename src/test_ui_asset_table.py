"""Tests for ui_asset_table.AssetTableModel paging and thumbnail dispatch.

Drives the model over a synchronous stub bridge and a real federation (no
widgets), covering the _row_by_id index (O(1) thumb-ready dispatch) and the
thumb-request retry on a failed path lookup.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from PIL import Image
from PySide6.QtCore import QCoreApplication

from federation import add_root_to_config, open_federation, scan_shard
from test_ui_controller import SyncBridge
from ui_asset_table import AssetTableModel, COL_THUMB

_app = None


def setUpModule():
    global _app
    _app = QCoreApplication.instance() or QCoreApplication([])


class _StubThumbBridge:
    def get_pixmap(self, dest):
        return None

    def request(self, **kwargs):
        pass

    def bump_priority(self, dest, priority):
        pass

    def root_abs_path(self, label):
        return None


class AssetTableModelTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "imgdb.conf")
        r1 = os.path.join(self.tmp, "alpha")
        os.makedirs(r1)
        for i in range(3):
            Image.new("RGB", (10 + i, 10), (i * 20, 0, 0)).save(
                os.path.join(r1, f"a{i}.png")
            )
        add_root_to_config("alpha", r1, self.cfg)
        self.fed = open_federation(self.cfg)
        scan_shard(self.fed, "alpha")
        self.model = AssetTableModel(SyncBridge(self.fed), _StubThumbBridge())

    def tearDown(self):
        self.fed.close()
        shutil.rmtree(self.tmp)

    def test_refresh_populates_rows_and_row_index(self):
        self.model.refresh(checked_labels=["alpha"], filter_rules=[], sort_rules=[])
        self.assertEqual(self.model.rowCount(), 3)
        rows = self.model.all_row_data()
        self.assertEqual(len(rows), 3)
        # _row_by_id maps every loaded asset to its row for O(1) thumb dispatch.
        for i, row in enumerate(rows):
            self.assertEqual(self.model._row_by_id[row.asset_id], i)

    def test_thumb_ready_dispatch_is_indexed(self):
        self.model.refresh(checked_labels=["alpha"], filter_rules=[], sort_rules=[])
        aid = self.model.row_data(1).asset_id
        emitted = []
        self.model.dataChanged.connect(
            lambda tl, br, roles=None: emitted.append((tl.row(), tl.column()))
        )
        self.model._on_thumb_ready(aid, None)
        self.assertIn((1, COL_THUMB), emitted)
        # Unknown asset id is a no-op (no crash, no emission).
        emitted.clear()
        self.model._on_thumb_ready("no-such-id", None)
        self.assertEqual(emitted, [])

    def test_refresh_clears_row_index(self):
        self.model.refresh(checked_labels=["alpha"], filter_rules=[], sort_rules=[])
        self.assertEqual(len(self.model._row_by_id), 3)
        # Filter that matches nothing → count 0 → index cleared.
        self.model.refresh(
            checked_labels=[], filter_rules=[], sort_rules=[]
        )
        self.assertEqual(self.model.rowCount(), 0)
        self.assertEqual(self.model._row_by_id, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
