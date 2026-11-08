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

from qt_test_app import ensure_qapp
import federation
import imgdb
from federation import add_root_to_config, open_federation, scan_shard
import captioner
from ui_controller import AppController, AutoCaptionController, AutoCaptionRequest

_app = None


def setUpModule():
    global _app
    _app = ensure_qapp()


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
        self.ctl.assets_changed.connect(lambda p: self.signals.append(f"assets:{p}"))
        self.ctl.datasets_changed.connect(lambda: self.signals.append("datasets"))
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

    def test_batch_add_tag_writes_and_refreshes(self):
        ids = self._ids()
        self.ctl.batch_add_tag(ids, ["x", "y"], "General")
        for aid in ids:
            self.assertEqual(self._tag_count(aid, "x"), 1)
            self.assertEqual(self._tag_count(aid, "y"), 1)
        # Must emit tags_changed so the tag-management counts refresh.
        self.assertIn("tags_changed", self.signals)

    def test_batch_remove_and_replace_emit_tags_changed(self):
        ids = self._ids()
        federation.add_tags(self.fed, ids[0], ["old"])
        self.signals.clear()
        self.ctl.batch_remove_tag(ids, "old")
        self.assertIn("tags_changed", self.signals)
        self.signals.clear()
        self.ctl.batch_add_tag(ids, ["a"], "General")
        self.ctl.batch_replace_tag(ids, "a", "b")
        self.assertIn("tags_changed", self.signals)
        self.assertEqual(self._tag_count(ids[0], "b"), 1)

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

    def test_rename_asset_emits_assets_changed_no_preserve(self):
        aid = self._ids()[0]
        old = imgdb.get_asset(self.conn, aid).rel_path
        new_rel = "renamed_" + os.path.basename(old)
        self.ctl.rename_asset(aid, new_rel)
        self.assertEqual(imgdb.get_asset(self.conn, aid).rel_path, new_rel)
        self.assertIn("assets:False", self.signals)

    def test_delete_assets_emits_assets_changed_preserve(self):
        aid = self._ids()[0]
        self.ctl.delete_assets([aid])
        self.assertNotIn(aid, self.fed.asset_index)
        self.assertIn("assets:True", self.signals)

    def test_remove_from_dataset_emits_both(self):
        ids = self._ids()
        federation.add_to_dataset(self.fed, "myset", ids)
        self.ctl.remove_from_dataset("myset", ids)
        self.assertIn("datasets", self.signals)
        self.assertIn("assets:True", self.signals)


class FakeCaptionServer:
    """Stands in for captioner.LlamaServer. `fail` maps a file basename to the
    exception caption() raises for it."""

    def __init__(self):
        self.loaded = None
        self.loads: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.fail: dict[str, BaseException] = {}

    def load(self, spec):
        if self.loaded != spec.name:
            self.loads.append(spec.name)
            self.loaded = spec.name

    def caption(self, path, prompt, params):
        name = os.path.basename(path)
        self.calls.append((name, prompt))
        if name in self.fail:
            raise self.fail[name]
        return f"caption of {name}"

    def unload(self):
        self.loaded = None


class ManualBridge:
    """Queues jobs instead of running them, so a test can act mid-run."""

    def __init__(self):
        self.jobs: list = []

    def submit(self, fn, *args, on_result=None, on_error=None, **kwargs):
        self.jobs.append((fn, on_result, on_error))

    def run_one(self):
        fn, on_result, on_error = self.jobs.pop(0)
        try:
            result = fn(None)
        except BaseException as e:
            if on_error:
                on_error(e)
            return
        if on_result:
            on_result(result)


class AutoCaptionControllerTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "imgdb.conf")
        r1 = os.path.join(self.tmp, "alpha")
        os.makedirs(r1)
        for i in range(3):
            Image.new("RGB", (10 + i, 10)).save(os.path.join(r1, f"a{i}.png"))
        add_root_to_config("alpha", r1, self.cfg)
        self.fed = open_federation(self.cfg)
        scan_shard(self.fed, "alpha")
        self.conn = self.fed.shards["alpha"].conn
        self.ids = sorted(
            (aid for aid, lbl in self.fed.asset_index.items() if lbl == "alpha"),
            key=lambda a: imgdb.get_asset(self.conn, a).rel_path,
        )

        self.server = FakeCaptionServer()
        self.spec = captioner.ModelSpec("M", "/m.gguf", "/p.gguf")
        self.ctl = AutoCaptionController(SyncBridge(self.fed), SyncBridge(None), self.server)
        self.ctl.set_models([self.spec])
        self.events: list = []
        self.ctl.caption_written.connect(lambda a, k: self.events.append(("written", a, k)))
        self.ctl.finished.connect(lambda *r: self.events.append(("finished",) + r))
        self.ctl.model_state_changed.connect(lambda m, st: self.events.append(("state", m, st)))
        self.ctl.running_changed.connect(lambda r: self.events.append(("running", r)))
        self.errors: list = []
        self.ctl.error.connect(self.errors.append)

    def tearDown(self):
        self.fed.close()
        shutil.rmtree(self.tmp)

    def _req(self, overwrite=True, kind="auto"):
        return AutoCaptionRequest("M", kind, "describe", captioner.CaptionParams(), overwrite)

    def _caption(self, aid, kind="auto"):
        return imgdb.get_captions_for_asset(self.conn, aid).get(kind, (None,))[0]

    def _finished(self):
        return [e[1:] for e in self.events if e[0] == "finished"]

    def test_captions_all_and_loads_model_once(self):
        self.ctl.enqueue(self.ids, self._req())
        for i, aid in enumerate(self.ids):
            self.assertEqual(self._caption(aid), f"caption of a{i}.png")
        self.assertEqual(self.server.loads, ["M"])
        self.assertEqual(self._finished(), [(3, 0, 0, False)])
        self.assertIn(("state", "M", "loading"), self.events)
        self.assertIn(("state", "M", "ready"), self.events)
        self.assertEqual(self.events[0], ("running", True))
        self.assertEqual(self.events[-1], ("running", False))
        self.assertFalse(self.ctl.is_running)

    def test_skip_mode_keeps_existing(self):
        federation.set_caption(self.fed, self.ids[0], "auto", "human written")
        self.ctl.enqueue(self.ids, self._req(overwrite=False))
        self.assertEqual(self._caption(self.ids[0]), "human written")
        self.assertEqual(self._caption(self.ids[1]), "caption of a1.png")
        self.assertEqual(self._finished(), [(2, 1, 0, False)])
        self.assertNotIn("a0.png", [c[0] for c in self.server.calls])

    def test_overwrite_mode_replaces_existing(self):
        federation.set_caption(self.fed, self.ids[0], "auto", "old")
        self.ctl.enqueue(self.ids[:1], self._req(overwrite=True))
        self.assertEqual(self._caption(self.ids[0]), "caption of a0.png")

    def test_other_kinds_untouched(self):
        federation.set_caption(self.fed, self.ids[0], "manual", "keep me")
        self.ctl.enqueue(self.ids[:1], self._req(kind="auto"))
        self.assertEqual(self._caption(self.ids[0], "manual"), "keep me")

    def test_image_error_skips_one_and_continues(self):
        self.server.fail["a1.png"] = captioner.ImageError("corrupt")
        self.ctl.enqueue(self.ids, self._req())
        self.assertIsNone(self._caption(self.ids[1]))
        self.assertEqual(self._caption(self.ids[2]), "caption of a2.png")
        self.assertEqual(self._finished(), [(2, 0, 1, False)])
        self.assertEqual(self.errors, [])

    def test_server_error_aborts_run(self):
        self.server.fail["a0.png"] = captioner.ServerError("boom")
        self.ctl.enqueue(self.ids, self._req())
        self.assertEqual(len(self.server.calls), 1)
        self.assertEqual(len(self.errors), 1)
        self.assertIn(("state", "M", "error"), self.events)
        self.assertEqual(self._finished(), [(0, 0, 1, False)])
        self.assertFalse(self.ctl.is_running)

    def test_unknown_model_errors_without_running(self):
        self.ctl.enqueue(self.ids, AutoCaptionRequest(
            "Nope", "auto", "p", captioner.CaptionParams(), True))
        self.assertEqual(len(self.errors), 1)
        self.assertFalse(self.ctl.is_running)

    def test_cancel_finishes_in_flight_and_drops_rest(self):
        cap = ManualBridge()
        ctl = AutoCaptionController(SyncBridge(self.fed), cap, self.server)
        ctl.set_models([self.spec])
        finished = []
        ctl.finished.connect(lambda *r: finished.append(r))
        ctl.enqueue(self.ids, self._req())
        self.assertEqual(len(cap.jobs), 1)       # a0 generating
        ctl.cancel()
        self.assertEqual(finished, [])           # still in flight
        cap.run_one()
        self.assertEqual(self._caption(self.ids[0]), "caption of a0.png")
        self.assertIsNone(self._caption(self.ids[1]))
        self.assertEqual(finished, [(1, 0, 0, True)])
        self.assertFalse(ctl.is_running)

    def test_enqueue_during_run_appends_and_dedupes(self):
        cap = ManualBridge()
        ctl = AutoCaptionController(SyncBridge(self.fed), cap, self.server)
        ctl.set_models([self.spec])
        progress = []
        ctl.progress.connect(lambda d, t, n: progress.append((d, t)))
        ctl.enqueue(self.ids[:2], self._req())
        ctl.enqueue(self.ids, self._req())       # a0, a1 already queued -> only a2 added
        self.assertEqual(progress[-1], (0, 3))
        while cap.jobs:
            cap.run_one()
        self.assertEqual([c[0] for c in self.server.calls], ["a0.png", "a1.png", "a2.png"])

    def test_unload_ignored_while_running(self):
        cap = ManualBridge()
        ctl = AutoCaptionController(SyncBridge(self.fed), cap, self.server)
        ctl.set_models([self.spec])
        ctl.enqueue(self.ids[:1], self._req())
        ctl.unload()
        self.assertEqual(len(cap.jobs), 1)       # only the generate job
        cap.run_one()
        ctl.unload()
        cap.run_one()
        self.assertIsNone(self.server.loaded)
        self.assertIsNone(ctl.loaded_model)


if __name__ == "__main__":
    unittest.main(verbosity=2)
