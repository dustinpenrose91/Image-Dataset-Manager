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
            CREATE TABLE datasets (
                name        TEXT PRIMARY KEY,
                description TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO assets (asset_id, rel_path, current_hash, phash)
                VALUES ('id1', 'a.png', 'HHH', 'PPP');
            INSERT INTO captions (asset_id, kind, content)
                VALUES ('id1', 'short', 'hello');
            INSERT INTO datasets (name) VALUES ('legacy');
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

    def test_open_existing_shard_missing_dataset_id_column(self):
        """Regression: opening a pre-dataset_id shard through init_shard (which
        runs executescript(SCHEMA_SQL) then _migrate) must not fail. The index
        must live in _migrate, not SCHEMA_SQL, or executescript aborts with
        'no such column: dataset_id' on the existing table."""
        import os
        import shutil
        import tempfile

        root = tempfile.mkdtemp()
        try:
            conn = imgdb.connect(imgdb.shard_db_path(root))
            conn.executescript(
                "CREATE TABLE datasets ("
                "  name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '');"
                "INSERT INTO datasets(name) VALUES ('legacy');"
            )
            conn.close()

            conn = imgdb.init_shard(root)  # full open path — must not raise
            cols = {r[1] for r in conn.execute("PRAGMA table_info(datasets)")}
            self.assertIn("dataset_id", cols)
            ds_id = conn.execute(
                "SELECT dataset_id FROM datasets WHERE name = 'legacy'"
            ).fetchone()["dataset_id"]
            self.assertTrue(ds_id)
            # Unique index exists (only created in _migrate now).
            idx = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_datasets_dataset_id'"
            ).fetchone()
            self.assertIsNotNone(idx)
            # New EAV table appears with no migration logic — just CREATE TABLE
            # IF NOT EXISTS in SCHEMA_SQL. This is the pattern that lets future
            # per-image data land without a schema migration.
            tbl = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='image_attributes'"
            ).fetchone()
            self.assertIsNotNone(tbl)
            conn.close()
        finally:
            shutil.rmtree(root)

    def test_dataset_id_backfilled_and_stable(self):
        conn = self._old_shard()
        imgdb._migrate(conn)
        ds_id = conn.execute(
            "SELECT dataset_id FROM datasets WHERE name = 'legacy'"
        ).fetchone()["dataset_id"]
        self.assertTrue(ds_id)
        # Backfill runs on every open but only fills NULLs — the id must not
        # be regenerated (pins reference it).
        imgdb._migrate(conn)
        again = conn.execute(
            "SELECT dataset_id FROM datasets WHERE name = 'legacy'"
        ).fetchone()["dataset_id"]
        self.assertEqual(again, ds_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
