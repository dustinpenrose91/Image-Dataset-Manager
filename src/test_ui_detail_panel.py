"""Functional test for the detail panel's Favorite control: loads state from
the DB, toggling emits favorite_changed and shows/hides the star."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from PIL import Image

from qt_test_app import ensure_qapp
import federation
import imgdb
from federation import add_root_to_config, open_federation, scan_shard
from test_ui_asset_table import _StubThumbBridge
from test_ui_controller import SyncBridge
from ui_detail_panel import DetailPanel

_app = None


def setUpModule():
    global _app
    _app = ensure_qapp()


class FavoriteControlTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cfg = os.path.join(self.tmp, "imgdb.conf")
        r1 = os.path.join(self.tmp, "alpha")
        os.makedirs(r1)
        Image.new("RGB", (12, 12), (1, 2, 3)).save(os.path.join(r1, "a.png"))
        add_root_to_config("alpha", r1, cfg)
        self.fed = open_federation(cfg)
        scan_shard(self.fed, "alpha")
        self.conn = self.fed.shards["alpha"].conn
        self.asset = next(federation.list_filtered_assets(self.fed, None, [], []))

        self.panel = DetailPanel(SyncBridge(self.fed), _StubThumbBridge())
        self.emitted = []
        self.panel.favorite_changed.connect(
            lambda aid, on: self.emitted.append((aid, on))
        )

    def tearDown(self):
        self.fed.close()
        shutil.rmtree(self.tmp)

    def _single(self):
        return self.panel._single

    def test_loads_favorite_state_from_db(self):
        imgdb.set_image_flag(self.conn, self.asset.asset_id, imgdb.ATTR_IS_FAVORITE, True)
        self.panel.load_selection([self.asset], self.fed)
        self.assertTrue(self._single()._favorite_btn.isChecked())
        self.assertFalse(self._single()._fav_star.isHidden())

    def test_toggle_emits_and_updates_star(self):
        self.panel.load_selection([self.asset], self.fed)  # not favorited
        s = self._single()
        self.assertTrue(s._fav_star.isHidden())

        s._favorite_btn.click()  # toggles checked -> True, fires clicked
        self.assertEqual(self.emitted[-1], (self.asset.asset_id, True))
        self.assertFalse(s._fav_star.isHidden())
        self.assertEqual(s._favorite_btn.text(), "★ Favorited")

        s._favorite_btn.click()  # back to False
        self.assertEqual(self.emitted[-1], (self.asset.asset_id, False))
        self.assertTrue(s._fav_star.isHidden())

    def test_star_is_child_of_thumbnail_not_in_layout(self):
        # The star overlays the thumbnail at the top-left, so it never shifts
        # the center-aligned image.
        s = self._single()
        self.assertIs(s._fav_star.parent(), s._thumb_label)
        self.assertEqual((s._fav_star.x(), s._fav_star.y()), (4, 4))


if __name__ == "__main__":
    unittest.main(verbosity=2)
