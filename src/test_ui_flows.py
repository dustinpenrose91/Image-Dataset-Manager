"""Tests for ui_flows.CaptionImportFlow.

Dialogs are stubbed so the fetch → dialog → import wiring runs headless over a
synchronous bridge and a real federation. The federation-level import functions
themselves are covered in test_federation_ops.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from PIL import Image
from PySide6.QtWidgets import QDialog

from qt_test_app import ensure_qapp
import federation
import ui_flows
from federation import add_root_to_config, open_federation, scan_shard, set_caption
from test_ui_controller import SyncBridge

_app = None


def setUpModule():
    global _app
    _app = ensure_qapp()


class _StubImportDialog:
    """Stands in for ImportFromCaptionDialog; returns preset choices."""
    scope_value = "single"
    selected = [("sunset", "General")]

    def __init__(self, **kwargs):
        pass

    def exec(self):
        return QDialog.DialogCode.Accepted

    def scope(self):
        return self.scope_value

    def caption_kind(self):
        return "short"

    def ambiguous_policy(self):
        return "general"

    def selected_tags(self):
        return self.selected


class CaptionImportFlowTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "imgdb.conf")
        r1 = os.path.join(self.tmp, "alpha")
        os.makedirs(r1)
        Image.new("RGB", (10, 10), (0, 0, 0)).save(os.path.join(r1, "a.png"))
        add_root_to_config("alpha", r1, self.cfg)
        self.fed = open_federation(self.cfg)
        scan_shard(self.fed, "alpha")
        self.conn = self.fed.shards["alpha"].conn
        self.aid = next(iter(self.fed.asset_index))
        set_caption(self.fed, self.aid, "short", "a golden sunset")

        self._orig_dialog = ui_flows.ImportFromCaptionDialog
        ui_flows.ImportFromCaptionDialog = _StubImportDialog
        self.refreshed = []

    def tearDown(self):
        ui_flows.ImportFromCaptionDialog = self._orig_dialog
        self.fed.close()
        shutil.rmtree(self.tmp)

    def test_single_scope_writes_selected_tags_and_refreshes(self):
        flow = ui_flows.CaptionImportFlow(
            bridge=SyncBridge(self.fed),
            parent=None,
            checked_labels=["alpha"],
            filter_rules=[],
            on_tags_changed=lambda: self.refreshed.append(True),
            on_error=lambda e: self.fail(f"unexpected error: {e}"),
        )
        flow.start(self.aid)

        n = self.conn.execute(
            "SELECT COUNT(*) FROM asset_tags at JOIN tags t ON t.tag_id = at.tag_id "
            "WHERE at.asset_id = ? AND t.name = 'sunset'",
            (self.aid,),
        ).fetchone()[0]
        self.assertEqual(n, 1)
        self.assertTrue(self.refreshed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
