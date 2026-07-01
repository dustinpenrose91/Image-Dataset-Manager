"""Tests for imgdb.py — per-shard shard-layer operations."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from PIL import Image

import imgdb
from imgdb import (
    init_shard, connect, hash_file, probe_image, scan_root,
    add_tags, remove_tags, set_caption, delete_caption, search_captions,
    rename_asset, delete_asset, merge_assets, resolve_merged_id,
    get_asset, transaction, shard_db_path,
    AssetNotFoundError, CaptionNotFoundError,
    MergeError, FileOperationError,
)


def _img(path: str, size=(10, 10), color=(200, 100, 50)) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", size, color).save(path)


class InitTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_creates_all_schema_tables(self):
        conn = init_shard(self.tmp)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )}
        for expected in ("assets", "captions", "tags", "asset_tags",
                         "asset_hash_history", "merged_assets"):
            with self.subTest(table=expected):
                self.assertIn(expected, tables)
        conn.close()

    def test_idempotent(self):
        conn = init_shard(self.tmp)
        conn.close()
        conn = init_shard(self.tmp)  # no exception; schema already exists
        conn.close()

    def test_read_only_connection_blocks_writes(self):
        conn_rw = init_shard(self.tmp)
        conn_rw.close()
        conn_ro = connect(shard_db_path(self.tmp), read_only=True)
        with self.assertRaises(Exception):
            conn_ro.execute("CREATE TABLE _probe (x INTEGER)")
        conn_ro.close()


class HashFileTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _write(self, name: str, data: bytes) -> str:
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_same_content_same_hash(self):
        a = self._write("a.bin", b"hello world")
        b = self._write("b.bin", b"hello world")
        self.assertEqual(hash_file(a), hash_file(b))

    def test_different_content_different_hash(self):
        a = self._write("a.bin", b"content a")
        b = self._write("b.bin", b"content b")
        self.assertNotEqual(hash_file(a), hash_file(b))

    def test_returns_hex_string(self):
        p = self._write("x.bin", b"data")
        h = hash_file(p)
        self.assertIsInstance(h, str)
        self.assertTrue(all(c in "0123456789abcdef" for c in h))


class ProbeImageTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_returns_dimensions_and_format(self):
        path = os.path.join(self.tmp, "img.png")
        Image.new("RGB", (80, 60)).save(path)
        r = probe_image(path)
        self.assertEqual(r.width, 80)
        self.assertEqual(r.height, 60)
        self.assertEqual(r.format, "PNG")
        self.assertGreater(r.bytes, 0)

    def test_non_image_returns_none_dims(self):
        path = os.path.join(self.tmp, "data.bin")
        with open(path, "wb") as f:
            f.write(b"not an image at all")
        r = probe_image(path)
        self.assertIsNone(r.width)
        self.assertIsNone(r.height)
        self.assertIsNone(r.format)
        self.assertEqual(r.bytes, 19)


class ScanTests(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.conn = init_shard(self.root)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root)

    def _put(self, rel: str, size=(10, 10), color=(0, 0, 0)) -> str:
        abs_path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        Image.new("RGB", size, color).save(abs_path)
        return abs_path

    def test_new_file(self):
        self._put("a.png")
        summary, new_ids = scan_root(self.conn, self.root)
        self.assertEqual(summary.new, 1)
        self.assertEqual(summary.unchanged, 0)
        self.assertEqual(summary.edited, 0)
        self.assertEqual(summary.missing, 0)
        self.assertEqual(len(new_ids), 1)

    def test_unchanged_on_rescan(self):
        self._put("a.png")
        scan_root(self.conn, self.root)
        summary, new_ids = scan_root(self.conn, self.root)
        self.assertEqual(summary.unchanged, 1)
        self.assertEqual(summary.new, 0)
        self.assertEqual(new_ids, [])

    def test_unchanged_when_only_mtime_changes(self):
        # Stat fast-path miss (mtime changed) but content hash matches → unchanged.
        abs_path = self._put("a.png")
        scan_root(self.conn, self.root)
        os.utime(abs_path, None)
        summary, _ = scan_root(self.conn, self.root)
        self.assertEqual(summary.unchanged, 1)
        self.assertEqual(summary.edited, 0)

    def test_edited_on_content_change(self):
        abs_path = self._put("a.png", color=(100, 0, 0))
        scan_root(self.conn, self.root)
        Image.new("RGB", (10, 10), (200, 50, 50)).save(abs_path)
        summary, _ = scan_root(self.conn, self.root)
        self.assertEqual(summary.edited, 1)
        self.assertEqual(summary.new, 0)

    def test_edited_moves_old_hash_to_history(self):
        abs_path = self._put("a.png", color=(100, 0, 0))
        scan_root(self.conn, self.root)
        old_hash = self.conn.execute(
            "SELECT file_hash FROM assets WHERE rel_path = 'a.png'"
        ).fetchone()["file_hash"]
        Image.new("RGB", (10, 10), (200, 50, 50)).save(abs_path)
        scan_root(self.conn, self.root)
        history = [r["hash"] for r in self.conn.execute(
            "SELECT hash FROM asset_hash_history"
        )]
        self.assertIn(old_hash, history)

    def test_missing_on_file_removal(self):
        abs_path = self._put("a.png")
        scan_root(self.conn, self.root)
        os.remove(abs_path)
        summary, _ = scan_root(self.conn, self.root)
        self.assertEqual(summary.missing, 1)
        row = self.conn.execute(
            "SELECT exists_flag FROM assets WHERE rel_path = 'a.png'"
        ).fetchone()
        self.assertEqual(row["exists_flag"], 0)

    def test_extension_filter_ignores_non_images(self):
        self._put("img.png")
        with open(os.path.join(self.root, "readme.txt"), "w") as f:
            f.write("ignored")
        summary, _ = scan_root(self.conn, self.root)
        self.assertEqual(summary.new, 1)

    def test_thumbs_dir_excluded(self):
        self._put("a.png")
        scan_root(self.conn, self.root)
        thumbs_subdir = os.path.join(self.root, imgdb.THUMBS_DIRNAME, "ab")
        os.makedirs(thumbs_subdir)
        Image.new("RGB", (10, 10)).save(os.path.join(thumbs_subdir, "fake.png"))
        summary, _ = scan_root(self.conn, self.root)
        self.assertEqual(summary.unchanged, 1)
        self.assertEqual(summary.new, 0)

    def test_mixed_states_in_one_scan(self):
        a = self._put("a.png", color=(10, 0, 0))
        self._put("b.png", color=(20, 0, 0))
        self._put("c.png", color=(30, 0, 0))
        scan_root(self.conn, self.root)
        Image.new("RGB", (10, 10), (255, 0, 0)).save(a)
        os.remove(os.path.join(self.root, "b.png"))
        summary, _ = scan_root(self.conn, self.root)
        self.assertEqual(summary.new, 0)
        self.assertEqual(summary.edited, 1)
        self.assertEqual(summary.unchanged, 1)
        self.assertEqual(summary.missing, 1)

    def test_asset_id_stable_across_edit(self):
        self._put("a.png", color=(10, 0, 0))
        _, ids = scan_root(self.conn, self.root)
        original_id = ids[0]
        Image.new("RGB", (10, 10), (255, 0, 0)).save(
            os.path.join(self.root, "a.png")
        )
        scan_root(self.conn, self.root)
        row = self.conn.execute(
            "SELECT asset_id FROM assets WHERE rel_path = 'a.png'"
        ).fetchone()
        self.assertEqual(row["asset_id"], original_id)


class TagTests(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.conn = init_shard(self.root)
        Image.new("RGB", (10, 10)).save(os.path.join(self.root, "a.png"))
        _, ids = scan_root(self.conn, self.root)
        self.asset_id = ids[0]

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root)

    def test_add_tag(self):
        add_tags(self.conn, self.asset_id, ["landscape"])
        row = self.conn.execute(
            """SELECT t.name FROM tags t
               JOIN asset_tags at ON at.tag_id = t.tag_id
               WHERE at.asset_id = ?""",
            (self.asset_id,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["name"].lower(), "landscape")

    def test_add_multiple_tags(self):
        add_tags(self.conn, self.asset_id, ["fog", "morning", "water"])
        count = self.conn.execute(
            "SELECT COUNT(*) FROM asset_tags WHERE asset_id = ?",
            (self.asset_id,)
        ).fetchone()[0]
        self.assertEqual(count, 3)

    def test_add_tag_idempotent(self):
        add_tags(self.conn, self.asset_id, ["foo"])
        add_tags(self.conn, self.asset_id, ["foo"])
        count = self.conn.execute(
            "SELECT COUNT(*) FROM asset_tags WHERE asset_id = ?",
            (self.asset_id,)
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_add_tag_case_insensitive(self):
        add_tags(self.conn, self.asset_id, ["Sunset"])
        add_tags(self.conn, self.asset_id, ["sunset"])
        tag_count = self.conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        self.assertEqual(tag_count, 1)

    def test_remove_tag(self):
        add_tags(self.conn, self.asset_id, ["landscape"])
        row = self.conn.execute("SELECT tag_id FROM tags WHERE name = 'landscape'").fetchone()
        remove_tags(self.conn, self.asset_id, [row["tag_id"]])
        count = self.conn.execute(
            "SELECT COUNT(*) FROM asset_tags WHERE asset_id = ?",
            (self.asset_id,)
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_remove_unknown_tag_id_is_silent(self):
        # remove_tags by tag_id silently ignores IDs not linked to the asset
        remove_tags(self.conn, self.asset_id, ["00000000-0000-0000-0000-000000000000"])

    def test_add_tag_unknown_asset_raises(self):
        with self.assertRaises(AssetNotFoundError):
            add_tags(self.conn, "00000000-0000-0000-0000-000000000000", ["tag"])


class CaptionTests(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.conn = init_shard(self.root)
        Image.new("RGB", (10, 10)).save(os.path.join(self.root, "a.png"))
        _, ids = scan_root(self.conn, self.root)
        self.asset_id = ids[0]

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root)

    def test_set_caption(self):
        set_caption(self.conn, self.asset_id, "short", "A nice photo")
        row = self.conn.execute(
            "SELECT content FROM captions WHERE asset_id = ? AND kind = 'short'",
            (self.asset_id,)
        ).fetchone()
        self.assertEqual(row["content"], "A nice photo")

    def test_set_caption_upsert(self):
        set_caption(self.conn, self.asset_id, "short", "first")
        set_caption(self.conn, self.asset_id, "short", "second")
        rows = self.conn.execute(
            "SELECT content FROM captions WHERE asset_id = ? AND kind = 'short'",
            (self.asset_id,)
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "second")

    def test_multiple_kinds(self):
        set_caption(self.conn, self.asset_id, "short", "brief")
        set_caption(self.conn, self.asset_id, "long", "a longer description")
        count = self.conn.execute(
            "SELECT COUNT(*) FROM captions WHERE asset_id = ?",
            (self.asset_id,)
        ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_delete_caption(self):
        set_caption(self.conn, self.asset_id, "short", "content")
        delete_caption(self.conn, self.asset_id, "short")
        count = self.conn.execute(
            "SELECT COUNT(*) FROM captions WHERE asset_id = ?",
            (self.asset_id,)
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_delete_nonexistent_caption_raises(self):
        with self.assertRaises(CaptionNotFoundError):
            delete_caption(self.conn, self.asset_id, "nonexistent")

    def test_fts_finds_match(self):
        set_caption(self.conn, self.asset_id, "long", "Sunset at the golden ridge")
        results = search_captions(self.conn, "sunset")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], self.asset_id)
        self.assertEqual(results[0][1], "long")

    def test_fts_no_match(self):
        set_caption(self.conn, self.asset_id, "long", "A rainy day in Portland")
        self.assertEqual(search_captions(self.conn, "sunshine"), [])

    def test_fts_updated_after_upsert(self):
        set_caption(self.conn, self.asset_id, "short", "original text here")
        set_caption(self.conn, self.asset_id, "short", "completely different words")
        self.assertEqual(search_captions(self.conn, "original"), [])
        self.assertEqual(len(search_captions(self.conn, "completely")), 1)

    def test_fts_cleared_after_delete(self):
        set_caption(self.conn, self.asset_id, "short", "deletable phrase here")
        delete_caption(self.conn, self.asset_id, "short")
        self.assertEqual(search_captions(self.conn, "deletable"), [])


class RenameTests(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.conn = init_shard(self.root)
        Image.new("RGB", (10, 10)).save(os.path.join(self.root, "a.png"))
        _, ids = scan_root(self.conn, self.root)
        self.asset_id = ids[0]

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root)

    def test_rename_moves_file_and_updates_db(self):
        rename_asset(self.conn, self.root, self.asset_id, "subdir/b.png")
        self.assertFalse(os.path.exists(os.path.join(self.root, "a.png")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "subdir", "b.png")))
        self.assertEqual(get_asset(self.conn, self.asset_id).rel_path, "subdir/b.png")

    def test_rename_same_path_is_noop(self):
        rename_asset(self.conn, self.root, self.asset_id, "a.png")
        self.assertTrue(os.path.exists(os.path.join(self.root, "a.png")))

    def test_rename_rejects_occupied_destination(self):
        Image.new("RGB", (10, 10)).save(os.path.join(self.root, "existing.png"))
        with self.assertRaises(FileOperationError):
            rename_asset(self.conn, self.root, self.asset_id, "existing.png")

    def test_rename_rejects_db_collision(self):
        # Second asset has rel_path "b.png" in DB; file deleted from disk so the
        # filesystem check passes, but the DB collision check must reject it.
        Image.new("RGB", (10, 10)).save(os.path.join(self.root, "b.png"))
        scan_root(self.conn, self.root)
        os.remove(os.path.join(self.root, "b.png"))
        with self.assertRaises(FileOperationError):
            rename_asset(self.conn, self.root, self.asset_id, "b.png")


class DeleteTests(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.conn = init_shard(self.root)
        Image.new("RGB", (10, 10)).save(os.path.join(self.root, "a.png"))
        _, ids = scan_root(self.conn, self.root)
        self.asset_id = ids[0]

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root)

    def test_delete_removes_file_and_row(self):
        delete_asset(self.conn, self.root, self.asset_id)
        self.assertFalse(os.path.exists(os.path.join(self.root, "a.png")))
        count = self.conn.execute(
            "SELECT COUNT(*) FROM assets WHERE asset_id = ?",
            (self.asset_id,)
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_delete_cascades_to_captions_and_tags(self):
        add_tags(self.conn, self.asset_id, ["tag1"])
        set_caption(self.conn, self.asset_id, "short", "desc")
        delete_asset(self.conn, self.root, self.asset_id)
        for table in ("captions", "asset_tags"):
            count = self.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE asset_id = ?",
                (self.asset_id,)
            ).fetchone()[0]
            with self.subTest(table=table):
                self.assertEqual(count, 0)

    def test_delete_with_file_missing_on_disk(self):
        os.remove(os.path.join(self.root, "a.png"))
        delete_asset(self.conn, self.root, self.asset_id)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM assets WHERE asset_id = ?",
            (self.asset_id,)
        ).fetchone()[0]
        self.assertEqual(count, 0)


class MergeTests(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.conn = init_shard(self.root)
        Image.new("RGB", (10, 10), (255, 0, 0)).save(os.path.join(self.root, "a.png"))
        Image.new("RGB", (10, 10), (0, 255, 0)).save(os.path.join(self.root, "b.png"))
        scan_root(self.conn, self.root)
        rows = self.conn.execute(
            "SELECT asset_id, rel_path FROM assets"
        ).fetchall()
        by_path = {r["rel_path"]: r["asset_id"] for r in rows}
        self.aid_a = by_path["a.png"]
        self.aid_b = by_path["b.png"]

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root)

    def test_merge_unions_tags(self):
        add_tags(self.conn, self.aid_a, ["landscape"])
        add_tags(self.conn, self.aid_b, ["sunset"])
        merge_assets(self.conn, self.aid_a, self.aid_b)
        tags = {r["name"] for r in self.conn.execute(
            """SELECT t.name FROM tags t
               JOIN asset_tags at ON at.tag_id = t.tag_id
               WHERE at.asset_id = ?""",
            (self.aid_a,)
        )}
        self.assertIn("landscape", tags)
        self.assertIn("sunset", tags)

    def test_merge_caption_longer_wins(self):
        set_caption(self.conn, self.aid_a, "short", "short")
        set_caption(self.conn, self.aid_b, "short", "a much longer description")
        merge_assets(self.conn, self.aid_a, self.aid_b)
        content = self.conn.execute(
            "SELECT content FROM captions WHERE asset_id = ? AND kind = 'short'",
            (self.aid_a,)
        ).fetchone()["content"]
        self.assertEqual(content, "a much longer description")

    def test_merge_caption_survivor_wins_tie(self):
        set_caption(self.conn, self.aid_a, "short", "same length xx")
        set_caption(self.conn, self.aid_b, "short", "same length yy")
        merge_assets(self.conn, self.aid_a, self.aid_b)
        content = self.conn.execute(
            "SELECT content FROM captions WHERE asset_id = ? AND kind = 'short'",
            (self.aid_a,)
        ).fetchone()["content"]
        self.assertEqual(content, "same length xx")

    def test_merge_transfers_merged_only_caption_kind(self):
        set_caption(self.conn, self.aid_b, "long", "exclusive to merged")
        merge_assets(self.conn, self.aid_a, self.aid_b)
        row = self.conn.execute(
            "SELECT content FROM captions WHERE asset_id = ? AND kind = 'long'",
            (self.aid_a,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["content"], "exclusive to merged")

    def test_merge_moves_hash_to_survivor_history(self):
        merged_hash = get_asset(self.conn, self.aid_b).file_hash
        merge_assets(self.conn, self.aid_a, self.aid_b)
        history = {r["hash"] for r in self.conn.execute(
            "SELECT hash FROM asset_hash_history WHERE asset_id = ?",
            (self.aid_a,)
        )}
        self.assertIn(merged_hash, history)

    def test_merge_old_id_resolves_to_survivor(self):
        merge_assets(self.conn, self.aid_a, self.aid_b)
        self.assertEqual(resolve_merged_id(self.conn, self.aid_b), self.aid_a)

    def test_merge_self_raises(self):
        with self.assertRaises(MergeError):
            merge_assets(self.conn, self.aid_a, self.aid_a)

    def test_resolve_merged_id_passthrough_for_current_id(self):
        self.assertEqual(resolve_merged_id(self.conn, self.aid_a), self.aid_a)

    def test_merge_removes_merged_asset_row(self):
        merge_assets(self.conn, self.aid_a, self.aid_b)
        with self.assertRaises(AssetNotFoundError):
            get_asset(self.conn, self.aid_b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
