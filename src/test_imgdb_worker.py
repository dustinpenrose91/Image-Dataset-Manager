"""Tests for imgdb_worker."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import shutil
import unittest

sys.path.insert(0, "/tmp/stubs")
sys.path.insert(0, "/home/claude/out")

from PIL import Image

import federation
import imgdb
import imgdb_worker
from imgdb_worker import DBWorker, call_sync


def _make_root_with_images(parent: str, name: str, n: int) -> str:
    root = os.path.join(parent, name)
    os.makedirs(root)
    for i in range(n):
        Image.new("RGB", (10, 10), (i * 30, 0, 0)).save(
            os.path.join(root, f"{name}_{i}.png")
        )
    return root


class WorkerTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "imgdb.conf")
        self.r1 = _make_root_with_images(self.tmp, "alpha", 3)
        self.r2 = _make_root_with_images(self.tmp, "beta", 2)
        federation.add_root_to_config("alpha", self.r1, self.cfg)
        federation.add_root_to_config("beta", self.r2, self.cfg)
        self.worker = DBWorker()

    def tearDown(self) -> None:
        try:
            self.worker.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp)

    def _start(self) -> None:
        self.worker.start(lambda: federation.open_federation(self.cfg))

    def test_startup_and_basic_call(self) -> None:
        self._start()
        summary = call_sync(self.worker, federation.scan_shard, "alpha")
        self.assertEqual(summary.new, 3)
        self.assertEqual(summary.unchanged, 0)

    def test_startup_failure_propagates(self) -> None:
        def bad_factory() -> federation.Federation:
            raise RuntimeError("simulated failure")

        with self.assertRaises(RuntimeError) as ctx:
            self.worker.start(bad_factory)
        self.assertIn("simulated failure", str(ctx.exception))
        # After a failed start, the worker is not running and submit fails.
        with self.assertRaises(RuntimeError):
            self.worker.submit(federation.scan_shard, "alpha")

    def test_error_callback_invoked(self) -> None:
        self._start()
        errors: list[BaseException] = []
        results: list = []
        done = threading.Event()

        def on_err(e: BaseException) -> None:
            errors.append(e)
            done.set()

        def on_ok(v) -> None:
            results.append(v)
            done.set()

        self.worker.submit(
            federation.scan_shard, "no-such-shard",
            on_result=on_ok, on_error=on_err,
        )
        self.assertTrue(done.wait(5.0))
        self.assertEqual(len(results), 0)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], imgdb.ImgDBError)

    def test_fifo_ordering(self) -> None:
        self._start()
        order: list[str] = []
        lock = threading.Lock()
        done = threading.Event()
        target = 4

        def make_cb(label: str) -> callable:
            def cb(_v) -> None:
                with lock:
                    order.append(label)
                    if len(order) == target:
                        done.set()
            return cb

        for label in ("a", "b", "c", "d"):
            self.worker.submit(
                federation.scan_shard, "alpha",
                on_result=make_cb(label),
            )
        self.assertTrue(done.wait(10.0))
        self.assertEqual(order, ["a", "b", "c", "d"])

    def test_cancel_before_run(self) -> None:
        self._start()
        # Block the worker on a long-running first job.
        gate = threading.Event()
        started = threading.Event()

        def slow_blocker(_fed) -> None:
            started.set()
            gate.wait(5.0)

        self.worker.submit(slow_blocker)
        self.assertTrue(started.wait(2.0))

        # Now queue two jobs and cancel one of them before the worker
        # gets to it.
        ran: list[str] = []
        done = threading.Event()
        h_keep = self.worker.submit(
            federation.scan_shard, "alpha",
            on_result=lambda v: ran.append("keep"),
        )
        h_cancel = self.worker.submit(
            federation.scan_shard, "alpha",
            on_result=lambda v: ran.append("cancel"),
        )
        self.assertTrue(h_cancel.cancel())
        self.assertFalse(h_cancel.cancel())  # idempotent

        # One more after the cancelled one so we know when the queue drained.
        self.worker.submit(
            federation.scan_shard, "alpha",
            on_result=lambda v: done.set(),
        )

        gate.set()  # release the blocker
        self.assertTrue(done.wait(10.0))
        self.assertIn("keep", ran)
        self.assertNotIn("cancel", ran)

    def test_shutdown_drains_pending(self) -> None:
        self._start()
        completed: list[int] = []
        done = threading.Event()
        target = 5

        def cb(i: int):
            def inner(_v):
                completed.append(i)
                if len(completed) == target:
                    done.set()
            return inner

        for i in range(target):
            self.worker.submit(
                federation.scan_shard, "alpha",
                on_result=cb(i),
            )
        self.worker.shutdown()
        # Shutdown is supposed to drain the queue first.
        self.assertEqual(len(completed), target)
        self.assertEqual(completed, list(range(target)))

    def test_double_shutdown_is_safe(self) -> None:
        self._start()
        self.worker.shutdown()
        self.worker.shutdown()  # no exception

    def test_check_same_thread_invariant(self) -> None:
        """
        SQLite connections opened with check_same_thread=True (the default)
        will raise if touched from a thread other than the one that opened
        them. Confirm that the worker really is the only thread touching
        the connection.
        """
        self._start()
        worker_thread_ids: list[int] = []

        def grab_tid(fed) -> int:
            tid = threading.get_ident()
            worker_thread_ids.append(tid)
            # Touch the connection to prove same-thread access works.
            list(fed.shards["alpha"].conn.execute("SELECT 1"))
            return tid

        tid = call_sync(self.worker, grab_tid)
        self.assertNotEqual(tid, threading.get_ident())
        self.assertEqual(len(worker_thread_ids), 1)

    def test_failing_callback_does_not_kill_worker(self) -> None:
        self._start()

        def boom(_v) -> None:
            raise RuntimeError("callback exploded")

        # A raising callback must be logged (not silently swallowed) and must
        # not take down the worker loop.
        with self.assertLogs("imgdb_worker", level="ERROR"):
            self.worker.submit(federation.scan_shard, "alpha", on_result=boom)
            # Worker should still be alive and processing.
            summary = call_sync(self.worker, federation.scan_shard, "beta")
        self.assertEqual(summary.new, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
