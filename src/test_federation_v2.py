"""Tests for federation pass: overlap check + filtered list helper."""
from __future__ import annotations

import os
import sys
import sqlite3
import shutil
import tempfile
import unittest

sys.path.insert(0, "/tmp/stubs")
sys.path.insert(0, "/home/claude/out")

from PIL import Image

import federation
import imgdb


def _make_root(parent: str, name: str, n: int) -> str:
    root = os.path.join(parent, name)
    os.makedirs(root)
    for i in range(n):
        Image.new("RGB", (10 + i, 10), (i * 30, 0, 0)).save(
            os.path.join(root, f"{name}_{i}.png")
        )
    return root


class OverlapTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "imgdb.conf")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def test_identical_root_rejected(self) -> None:
        r = _make_root(self.tmp, "alpha", 1)
        federation.add_root_to_config("alpha", r, self.cfg)
        with self.assertRaises(federation.OverlappingRootError):
            federation.add_root_to_config("beta", r, self.cfg)

    def test_child_inside_parent_rejected(self) -> None:
        outer = os.path.join(self.tmp, "outer")
        inner = os.path.join(outer, "inner")
        os.makedirs(inner)
        federation.add_root_to_config("outer", outer, self.cfg)
        with self.assertRaises(federation.OverlappingRootError):
            federation.add_root_to_config("inner", inner, self.cfg)

    def test_parent_around_child_rejected(self) -> None:
        outer = os.path.join(self.tmp, "outer")
        inner = os.path.join(outer, "inner")
        os.makedirs(inner)
        federation.add_root_to_config("inner", inner, self.cfg)
        with self.assertRaises(federation.OverlappingRootError):
            federation.add_root_to_config("outer", outer, self.cfg)

    def test_siblings_allowed(self) -> None:
        a = _make_root(self.tmp, "alpha", 1)
        b = _make_root(self.tmp, "beta", 1)
        federation.add_root_to_config("alpha", a, self.cfg)
        federation.add_root_to_config("beta", b, self.cfg)  # no error

    def test_similar_prefix_not_falsely_flagged(self) -> None:
        # /tmp/photos and /tmp/photos2 share a string prefix but neither
        # contains the other.
        a = os.path.join(self.tmp, "photos"); os.makedirs(a)
        b = os.path.join(self.tmp, "photos2"); os.makedirs(b)
        federation.add_root_to_config("a", a, self.cfg)
        federation.add_root_to_config("b", b, self.cfg)


class FilteredListTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "imgdb.conf")
        self.r1 = _make_root(self.tmp, "alpha", 4)
        self.r2 = _make_root(self.tmp, "beta", 3)
        federation.add_root_to_config("alpha", self.r1, self.cfg)
        federation.add_root_to_config("beta", self.r2, self.cfg)
        self.fed = federation.open_federation(self.cfg)
        federation.scan_shard(self.fed, "alpha")
        federation.scan_shard(self.fed, "beta")
        # Re-open so the read connection sees the freshly scanned data.
        self.fed.close()
        self.fed = federation.open_federation(self.cfg)

    def tearDown(self) -> None:
        self.fed.close()
        shutil.rmtree(self.tmp)

    def test_default_lists_all_visible_assets(self) -> None:
        rows = list(federation.list_filtered_assets(self.fed))
        self.assertEqual(len(rows), 7)
        # Sorted by rel_path ascending by default.
        rel_paths = [r.rel_path for r in rows]
        self.assertEqual(rel_paths, sorted(rel_paths))

    def test_count_matches_list(self) -> None:
        n = federation.count_filtered_assets(self.fed)
        rows = list(federation.list_filtered_assets(self.fed))
        self.assertEqual(n, len(rows))

    def test_filter_by_shard(self) -> None:
        rows = list(federation.list_filtered_assets(self.fed, checked_labels=["alpha"]))
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r.root == "alpha" for r in rows))

    def test_no_shards_checked_returns_empty(self) -> None:
        rows = list(federation.list_filtered_assets(self.fed, checked_labels=[]))
        self.assertEqual(rows, [])
        self.assertEqual(federation.count_filtered_assets(self.fed, checked_labels=[]), 0)

    def test_where_clause_applied(self) -> None:
        # Width was set to 10+i so beta_2 has width 12, etc.
        rows = list(federation.list_filtered_assets(
            self.fed, where_clause="width >= 12"))
        self.assertTrue(len(rows) >= 1)
        self.assertTrue(all(r.width >= 12 for r in rows))

    def test_where_clause_combines_with_shard_filter(self) -> None:
        rows = list(federation.list_filtered_assets(
            self.fed, checked_labels=["alpha"], where_clause="width >= 11"))
        self.assertTrue(all(r.root == "alpha" and r.width >= 11 for r in rows))

    def test_sort_descending(self) -> None:
        rows = list(federation.list_filtered_assets(self.fed, sort_desc=True))
        rel_paths = [r.rel_path for r in rows]
        self.assertEqual(rel_paths, sorted(rel_paths, reverse=True))

    def test_sort_by_bytes(self) -> None:
        rows = list(federation.list_filtered_assets(self.fed, sort_by="bytes"))
        sizes = [r.bytes for r in rows]
        self.assertEqual(sizes, sorted(sizes))

    def test_sort_by_invalid_column_rejected(self) -> None:
        with self.assertRaises(ValueError):
            list(federation.list_filtered_assets(self.fed, sort_by="rel_path; DROP TABLE assets"))

    def test_syntax_error_propagates(self) -> None:
        with self.assertRaises(sqlite3.OperationalError):
            list(federation.list_filtered_assets(self.fed, where_clause="not a valid clause"))

    def test_show_missing_excludes_by_default(self) -> None:
        # Mark one asset as missing manually.
        any_id = next(iter(self.fed.asset_index))
        shard = federation.shard_for_asset(self.fed, any_id)
        with imgdb.transaction(shard.conn):
            shard.conn.execute(
                "UPDATE assets SET exists_flag = 0 WHERE asset_id = ?", (any_id,)
            )
        # Re-open so the read conn sees it.
        self.fed.close()
        self.fed = federation.open_federation(self.cfg)
        visible = list(federation.list_filtered_assets(self.fed))
        with_missing = list(federation.list_filtered_assets(self.fed, show_missing=True))
        self.assertEqual(len(with_missing), len(visible) + 1)

    def test_streaming_does_not_materialize(self) -> None:
        # Smoke test: the function returns a generator, not a list.
        result = federation.list_filtered_assets(self.fed)
        self.assertFalse(isinstance(result, list))
        # First iteration produces a row.
        first = next(iter(result))
        self.assertIsInstance(first, federation.AssetRow)


if __name__ == "__main__":
    unittest.main(verbosity=2)
