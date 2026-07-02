"""Tests for ui_controller.AppController.

Uses a synchronous stub bridge (matching QtDBBridge.submit's contract) over a
real in-memory federation, so each intent's DB effect and emitted signal are
verified without a Qt event loop.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from PIL import Image
from PySide6.QtCore import QCoreApplication

import federation
import imgdb
from federation import add_root_to_config, open_federation, scan_shard
from ui_controller import AppController

_app = None


def setUpModule():
    global _app
    _app = QCoreApplication.instance() or QCoreApplication([])


class SyncBridge:
    """Runs fn(fed, ...) inline and invokes on_result/on_error synchronously."""

    def __init__(self, fed):
        self._fed = fed

    @property
    def is_running(self):
        return True

    def submit(self, fn, *args, on_result=None, on_error=None, **kwargs):
        try:
            result = fn(self._fed, *args, **kwargs)
        except BaseException as e:
            if on_error is not None:
                on_error(e)
            return
        if on_result is not None:
            on_result(result)


class ControllerTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "imgdb.conf")
        r1 = os.path.join(self.tmp, "alpha")
        os.makedirs(r1)
        for i in range(3):
            Image.new("RGB", (10 + i, 10), (i * 10, 0, 0)).save(
                os.path.join(r1, f"a{i}.png")
            )
        add_root_to_config("alpha", r1, self.cfg)
        self.fed = open_federation(self.cfg)
        scan_shard(self.fed, "alpha")
        self.conn = self.fed.shards["alpha"].conn

        self.ctl = AppController(SyncBridge(self.fed))
        self.signals: list[str] = []
        self.ctl.tags_changed.connect(lambda: self.signals.append("tags_changed"))
        self.ctl.tag_suggestions_stale.connect(lambda: self.signals.append("stale"))
        self.errors: list = []
        self.ctl.error.connect(self.errors.append)

    def tearDown(self):
        self.fed.close()
        shutil.rmtree(self.tmp)

    def _ids(self) -> list[str]:
        return [aid for aid, lbl in self.fed.asset_index.items() if lbl == "alpha"]

    def _tag_count(self, asset_id: str, name: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM asset_tags at JOIN tags t ON t.tag_id = at.tag_id "
            "WHERE at.asset_id = ? AND t.name = ?",
            (asset_id, name),
        ).fetchone()[0]

    def test_add_tag_writes_and_emits_stale_only(self):
        aid = self._ids()[0]
        self.ctl.add_tag(aid, "sky", "General")
        self.assertEqual(self._tag_count(aid, "sky"), 1)
        self.assertIn("stale", self.signals)
        self.assertNotIn("tags_changed", self.signals)

    def test_remove_tag_emits_stale(self):
        aid = self._ids()[0]
        federation.add_tags(self.fed, aid, ["sky"])
        tag_id = self.conn.execute(
            "SELECT tag_id FROM tags WHERE name = 'sky'"
        ).fetchone()["tag_id"]
        self.ctl.remove_tag(aid, tag_id)
        self.assertEqual(self._tag_count(aid, "sky"), 0)
        self.assertIn("stale", self.signals)

    def test_add_to_selection_emits_tags_changed(self):
        ids = self._ids()
        self.ctl.add_tag_to_selection(ids, "batchtag", "General")
        for aid in ids:
            self.assertEqual(self._tag_count(aid, "batchtag"), 1)
        self.assertIn("tags_changed", self.signals)
        self.assertNotIn("stale", self.signals)

    def test_batch_add_tag_writes_without_signal(self):
        ids = self._ids()
        self.ctl.batch_add_tag(ids, ["x", "y"], "General")
        for aid in ids:
            self.assertEqual(self._tag_count(aid, "x"), 1)
            self.assertEqual(self._tag_count(aid, "y"), 1)
        self.assertEqual(self.signals, [])

    def test_save_and_delete_caption(self):
        aid = self._ids()[0]
        self.ctl.save_caption(aid, "short", "hello")
        self.assertEqual(
            imgdb.get_captions_for_asset(self.conn, aid)["short"][0], "hello"
        )
        self.ctl.delete_caption(aid, "short")
        self.assertNotIn("short", imgdb.get_captions_for_asset(self.conn, aid))

    def test_replace_tag_globally_emits_tags_changed(self):
        aid = self._ids()[0]
        federation.add_tags(self.fed, aid, ["old"], type_name="General")
        self.ctl.replace_tag_globally("old", "General", "new", "General")
        self.assertEqual(self._tag_count(aid, "old"), 0)
        self.assertEqual(self._tag_count(aid, "new"), 1)
        self.assertIn("tags_changed", self.signals)

    def test_error_signal_on_bad_asset(self):
        self.ctl.add_tag("no-such-id", "sky", "General")
        self.assertTrue(self.errors)
        self.assertNotIn("stale", self.signals)


if __name__ == "__main__":
    unittest.main(verbosity=2)
