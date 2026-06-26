"""Tests for imgdb_thumbs."""
from __future__ import annotations

import os
import sys
import shutil
import tempfile
import threading
import time
import unittest

sys.path.insert(0, "/tmp/stubs")
sys.path.insert(0, "/home/claude/out")

from PIL import Image

import imgdb
import imgdb_thumbs
from imgdb_thumbs import (
    ThumbnailWorker, generate_thumbnail, thumb_path,
    PRIORITY_VISIBLE, PRIORITY_BACKGROUND, DEFAULT_THUMB_SIZE,
)


def _make_image(path: str, color=(255, 0, 0), size=(400, 300)) -> None:
    Image.new("RGB", size, color).save(path)


class GenerateTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def test_generate_basic(self) -> None:
        src = os.path.join(self.tmp, "src.png")
        dst = os.path.join(self.tmp, "thumbs", "ab", "x.webp")
        _make_image(src)
        generate_thumbnail(src, dst, size=64)
        self.assertTrue(os.path.exists(dst))
        with Image.open(dst) as im:
            self.assertEqual(im.format, "WEBP")
            self.assertLessEqual(max(im.size), 64)

    def test_generate_preserves_aspect_ratio(self) -> None:
        src = os.path.join(self.tmp, "wide.png")
        dst = os.path.join(self.tmp, "wide.webp")
        _make_image(src, size=(400, 100))
        generate_thumbnail(src, dst, size=128)
        with Image.open(dst) as im:
            self.assertEqual(im.size, (128, 32))

    def test_generate_atomic_no_partial_file(self) -> None:
        # If generation fails partway, the destination must not exist.
        src = os.path.join(self.tmp, "missing.png")  # doesn't exist
        dst = os.path.join(self.tmp, "out.webp")
        with self.assertRaises(Exception):
            generate_thumbnail(src, dst)
        self.assertFalse(os.path.exists(dst))
        self.assertFalse(os.path.exists(dst + ".tmp"))


class ThumbPathTests(unittest.TestCase):

    def test_path_uses_fanout_and_hash(self) -> None:
        p = thumb_path("/data/root", "abcdef12-3456-7890-abcd-ef1234567890",
                       "deadbeef" * 8)
        self.assertIn("/imgdb_thumbs/ab/", p)
        self.assertIn("abcdef12-3456-7890-abcd-ef1234567890-deadbeefdead", p)
        self.assertTrue(p.endswith(".webp"))

    def test_path_changes_when_hash_changes(self) -> None:
        aid = "abcdef12-3456-7890-abcd-ef1234567890"
        p1 = thumb_path("/data/root", aid, "a" * 64)
        p2 = thumb_path("/data/root", aid, "b" * 64)
        self.assertNotEqual(p1, p2)


class WorkerTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.worker = ThumbnailWorker()
        self.worker.start()

    def tearDown(self) -> None:
        try:
            self.worker.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp)

    def _src(self, name: str, color=(0, 128, 255)) -> str:
        p = os.path.join(self.tmp, name)
        _make_image(p, color=color)
        return p

    def test_basic_submit(self) -> None:
        src = self._src("a.png")
        dst = os.path.join(self.tmp, "out", "a.webp")
        ready = threading.Event()
        result: list = []

        def cb(aid, path):
            result.append((aid, path))
            ready.set()

        self.worker.submit("aid-1", src, dst, on_ready=cb)
        self.assertTrue(ready.wait(5.0))
        self.assertEqual(result[0][0], "aid-1")
        self.assertTrue(os.path.exists(dst))

    def test_cache_hit_is_synchronous_no_op(self) -> None:
        src = self._src("a.png")
        dst = os.path.join(self.tmp, "out.webp")
        generate_thumbnail(src, dst, size=64)  # pre-existing
        called: list = []
        self.worker.submit("aid-1", src, dst, on_ready=lambda a, p: called.append(a))
        # Should have fired synchronously, not via the worker.
        self.assertEqual(called, ["aid-1"])
        self.assertEqual(self.worker.queue_depth(), 0)

    def test_error_callback(self) -> None:
        bad_src = os.path.join(self.tmp, "nope.png")  # doesn't exist
        dst = os.path.join(self.tmp, "x.webp")
        err_seen = threading.Event()
        errors: list = []

        def on_err(aid, e):
            errors.append((aid, e))
            err_seen.set()

        self.worker.submit("aid-1", bad_src, dst, on_error=on_err)
        self.assertTrue(err_seen.wait(5.0))
        self.assertEqual(errors[0][0], "aid-1")

    def test_priority_visible_jumps_background(self) -> None:
        # Stop the worker, queue background jobs, queue a visible job,
        # then restart and confirm visible runs first.
        self.worker.shutdown()
        self.worker = ThumbnailWorker()

        order: list = []
        order_lock = threading.Lock()
        gate = threading.Event()
        target = 6  # 5 background + 1 visible

        def make_cb(label):
            def cb(aid, path):
                with order_lock:
                    order.append(label)
                    if len(order) == target:
                        gate.set()
            return cb

        # Queue 5 background jobs, then 1 visible.
        for i in range(5):
            src = self._src(f"bg{i}.png")
            dst = os.path.join(self.tmp, f"bg{i}.webp")
            self.worker.submit(f"bg-{i}", src, dst,
                               priority=PRIORITY_BACKGROUND,
                               on_ready=make_cb(f"bg{i}"))
        src_v = self._src("vis.png", color=(0, 255, 0))
        dst_v = os.path.join(self.tmp, "vis.webp")
        self.worker.submit("vis-1", src_v, dst_v,
                           priority=PRIORITY_VISIBLE,
                           on_ready=make_cb("VIS"))

        self.worker.start()
        self.assertTrue(gate.wait(15.0))
        self.assertEqual(order[0], "VIS")

    def test_dedup_drops_lower_priority_duplicate(self) -> None:
        self.worker.shutdown()
        self.worker = ThumbnailWorker()

        ran: list = []
        done = threading.Event()
        target = 1

        def cb(aid, path):
            ran.append(aid)
            if len(ran) == target:
                done.set()

        src = self._src("a.png")
        dst = os.path.join(self.tmp, "a.webp")
        self.worker.submit("aid-1", src, dst, priority=PRIORITY_VISIBLE, on_ready=cb)
        self.worker.submit("aid-1", src, dst, priority=PRIORITY_BACKGROUND, on_ready=cb)
        self.worker.submit("aid-1", src, dst, priority=PRIORITY_BACKGROUND, on_ready=cb)
        self.worker.start()
        self.assertTrue(done.wait(5.0))
        # Only the first (visible) job should fire its callback.
        self.assertEqual(ran, ["aid-1"])

    def test_dedup_promotes_to_higher_priority(self) -> None:
        self.worker.shutdown()
        self.worker = ThumbnailWorker()

        # Block the worker on a slow first job, queue several background
        # jobs, then promote one and verify it runs first when unblocked.
        gate = threading.Event()
        first_started = threading.Event()
        completed: list = []
        done = threading.Event()
        target = 4

        def slow_cb(aid, path):
            # Worker thread blocks here until released.
            first_started.set()
            gate.wait(5.0)
            completed.append(aid)
            if len(completed) == target:
                done.set()

        def cb(aid, path):
            completed.append(aid)
            if len(completed) == target:
                done.set()

        # First job: slow blocker.
        src0 = self._src("blocker.png")
        dst0 = os.path.join(self.tmp, "blocker.webp")
        self.worker.submit("aid-blocker", src0, dst0,
                           priority=PRIORITY_BACKGROUND, on_ready=slow_cb)
        self.worker.start()
        self.assertTrue(first_started.wait(5.0))

        # Three background jobs, then promote the third.
        srcs = []
        for i in range(3):
            s = self._src(f"x{i}.png", color=(i * 60, 0, 0))
            d = os.path.join(self.tmp, f"x{i}.webp")
            srcs.append((f"aid-{i}", s, d))
            self.worker.submit(f"aid-{i}", s, d,
                               priority=PRIORITY_BACKGROUND, on_ready=cb)
        # Promote aid-2 to visible.
        self.worker.submit("aid-2", srcs[2][1], srcs[2][2],
                           priority=PRIORITY_VISIBLE, on_ready=cb)

        gate.set()
        self.assertTrue(done.wait(10.0))
        # Order: blocker (already running), then aid-2 (promoted), then
        # aid-0 and aid-1 in some order.
        self.assertEqual(completed[0], "aid-blocker")
        self.assertEqual(completed[1], "aid-2")
        self.assertEqual(set(completed[2:]), {"aid-0", "aid-1"})

    def test_cancel_skips_pending_job(self) -> None:
        self.worker.shutdown()
        self.worker = ThumbnailWorker()

        gate = threading.Event()
        first_started = threading.Event()
        ran: list = []
        finished = threading.Event()

        def slow(aid, path):
            first_started.set()
            gate.wait(5.0)
            ran.append(aid)

        def cb(aid, path):
            ran.append(aid)
            if "aid-keep" in ran:
                finished.set()

        src0 = self._src("blocker.png")
        dst0 = os.path.join(self.tmp, "blocker.webp")
        self.worker.submit("aid-blocker", src0, dst0, on_ready=slow)
        self.worker.start()
        self.assertTrue(first_started.wait(5.0))

        s_cancel = self._src("c.png"); d_cancel = os.path.join(self.tmp, "c.webp")
        s_keep = self._src("k.png"); d_keep = os.path.join(self.tmp, "k.webp")
        self.worker.submit("aid-cancel", s_cancel, d_cancel, on_ready=cb)
        self.worker.submit("aid-keep", s_keep, d_keep, on_ready=cb)

        self.assertTrue(self.worker.cancel("aid-cancel"))
        gate.set()
        self.assertTrue(finished.wait(10.0))
        self.assertNotIn("aid-cancel", ran)
        self.assertIn("aid-keep", ran)

    def test_failing_callback_does_not_kill_worker(self) -> None:
        src = self._src("a.png")
        dst = os.path.join(self.tmp, "a.webp")
        self.worker.submit("aid-1", src, dst,
                           on_ready=lambda a, p: (_ for _ in ()).throw(RuntimeError("boom")))
        # Worker should still process subsequent jobs.
        done = threading.Event()
        src2 = self._src("b.png", color=(0, 255, 0))
        dst2 = os.path.join(self.tmp, "b.webp")
        self.worker.submit("aid-2", src2, dst2, on_ready=lambda a, p: done.set())
        self.assertTrue(done.wait(5.0))

    def test_shutdown_idempotent(self) -> None:
        self.worker.shutdown()
        self.worker.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
