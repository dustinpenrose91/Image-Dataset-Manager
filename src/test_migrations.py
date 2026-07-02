"""Tests for imgdb._migrate — incremental schema evolution on existing shards.

Builds a pre-migration ("old") schema, runs _migrate, and asserts the column
renames/additions land and existing row data survives.
"""
from __future__ import annotations

import unittest

import imgdb


class MigrateTests(unittest.TestCase):

    def _old_shard(self) -> "imgdb.sqlite3.Connection":
        """A shard predating file_hash/perceptual_hash renames and the
        tags_validated / has_mask / is_validated columns."""
        conn = imgdb.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE assets (
                asset_id TEXT PRIMARY KEY,
                rel_path TEXT,
                current_hash TEXT,
                phash TEXT,
                width INTEGER, height INTEGER, format TEXT, bytes INTEGER
            );
            CREATE TABLE captions (
                asset_id TEXT, kind TEXT, content TEXT
            );
            INSERT INTO assets (asset_id, rel_path, current_hash, phash)
                VALUES ('id1', 'a.png', 'HHH', 'PPP');
            INSERT INTO captions (asset_id, kind, content)
                VALUES ('id1', 'short', 'hello');
            """
        )
        return conn

    def test_renames_and_adds_columns_preserving_data(self):
        conn = self._old_shard()
        imgdb._migrate(conn)

        acols = {r[1] for r in conn.execute("PRAGMA table_info(assets)")}
        self.assertIn("file_hash", acols)
        self.assertNotIn("current_hash", acols)
        self.assertIn("perceptual_hash", acols)
        self.assertNotIn("phash", acols)
        self.assertIn("tags_validated", acols)
        self.assertIn("has_mask", acols)

        ccols = {r[1] for r in conn.execute("PRAGMA table_info(captions)")}
        self.assertIn("is_validated", ccols)

        row = conn.execute(
            "SELECT file_hash, perceptual_hash, tags_validated, has_mask "
            "FROM assets WHERE asset_id = 'id1'"
        ).fetchone()
        self.assertEqual(row["file_hash"], "HHH")
        self.assertEqual(row["perceptual_hash"], "PPP")
        # New flags default to 0 (not validated / no mask).
        self.assertEqual(row["tags_validated"], 0)
        self.assertEqual(row["has_mask"], 0)

    def test_migrate_is_idempotent(self):
        conn = self._old_shard()
        imgdb._migrate(conn)
        imgdb._migrate(conn)  # second pass must not raise or duplicate columns
        acols = [r[1] for r in conn.execute("PRAGMA table_info(assets)")]
        self.assertEqual(acols.count("file_hash"), 1)
        self.assertEqual(acols.count("perceptual_hash"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
