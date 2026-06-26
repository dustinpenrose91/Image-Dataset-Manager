"""Tests for federation.py — config management, routing, and write operations."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from PIL import Image

import imgdb
import federation
from federation import (
    _validate_label, load_config, save_config, RootEntry,
    add_root_to_config, remove_root_from_config,
    open_federation, attach_root, detach_root, list_roots,
    shard_for_asset, shard_by_label,
    scan_shard, add_tags, remove_tags, set_caption, delete_caption,
    rename_asset, delete_asset, merge_assets, search_captions,
    run_user_query,
    ConfigError, RootNotFoundError, RootAlreadyExistsError,
    ShardUnavailableError, CrossShardOperationError, FederationError,
)
from imgdb import AssetNotFoundError


def _make_root(parent: str, name: str, n: int = 2) -> str:
    root = os.path.join(parent, name)
    os.makedirs(root)
    for i in range(n):
        Image.new("RGB", (10 + i, 10), (i * 30, 0, 0)).save(
            os.path.join(root, f"{name}_{i}.png")
        )
    return root


class ValidateLabelTests(unittest.TestCase):

    def test_valid_labels(self):
        for label in ("alpha", "my_root", "_private", "Root2", "A"):
            with self.subTest(label=label):
                _validate_label(label)  # must not raise

    def test_empty_label_rejected(self):
        with self.assertRaises(ConfigError):
            _validate_label("")

    def test_reserved_label_main_rejected(self):
        with self.assertRaises(ConfigError):
            _validate_label("main")

    def test_reserved_label_temp_rejected(self):
        with self.assertRaises(ConfigError):
            _validate_label("temp")

    def test_label_starting_with_digit_rejected(self):
        with self.assertRaises(ConfigError):
            _validate_label("1bad")

    def test_label_with_hyphen_rejected(self):
        with self.assertRaises(ConfigError):
            _validate_label("my-root")

    def test_label_with_space_rejected(self):
        with self.assertRaises(ConfigError):
            _validate_label("my root")


class LoadSaveConfigTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "imgdb.conf")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_missing_file_returns_empty(self):
        self.assertEqual(load_config(self.cfg), [])

    def test_round_trip(self):
        entries = [
            RootEntry(label="alpha", abs_path="/data/alpha"),
            RootEntry(label="beta", abs_path="/data/beta"),
        ]
        save_config(entries, self.cfg)
        loaded = load_config(self.cfg)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0].label, "alpha")
        self.assertEqual(loaded[1].label, "beta")

    def test_add_root_then_load(self):
        r = _make_root(self.tmp, "root1", 0)
        add_root_to_config("root1", r, self.cfg)
        entries = load_config(self.cfg)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].label, "root1")

    def test_add_root_idempotent_same_path(self):
        r = _make_root(self.tmp, "r", 0)
        add_root_to_config("r", r, self.cfg)
        add_root_to_config("r", r, self.cfg)  # same label + path: no-op
        self.assertEqual(len(load_config(self.cfg)), 1)

    def test_rebind_label_to_different_path_rejected(self):
        r1 = _make_root(self.tmp, "r1", 0)
        r2 = _make_root(self.tmp, "r2", 0)
        add_root_to_config("myroot", r1, self.cfg)
        with self.assertRaises(RootAlreadyExistsError):
            add_root_to_config("myroot", r2, self.cfg)


class RemoveRootTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "imgdb.conf")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_remove_entry(self):
        r = _make_root(self.tmp, "alpha", 0)
        add_root_to_config("alpha", r, self.cfg)
        remove_root_from_config("alpha", self.cfg)
        self.assertEqual(load_config(self.cfg), [])

    def test_remove_nonexistent_raises(self):
        with self.assertRaises(RootNotFoundError):
            remove_root_from_config("nobody", self.cfg)

    def test_remove_one_of_two_leaves_other(self):
        r1 = _make_root(self.tmp, "a", 0)
        r2 = _make_root(self.tmp, "b", 0)
        add_root_to_config("a", r1, self.cfg)
        add_root_to_config("b", r2, self.cfg)
        remove_root_from_config("a", self.cfg)
        entries = load_config(self.cfg)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].label, "b")


class OpenFederationTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "imgdb.conf")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_empty_config_yields_empty_federation(self):
        with open_federation(self.cfg) as fed:
            self.assertEqual(fed.shards, {})
            self.assertEqual(fed.missing, {})

    def test_missing_root_triggers_warning_and_continues(self):
        save_config(
            [RootEntry(label="ghost", abs_path=os.path.join(self.tmp, "gone"))],
            self.cfg,
        )
        warnings = []
        with open_federation(self.cfg, on_warning=warnings.append) as fed:
            self.assertEqual(len(warnings), 1)
            self.assertIn("ghost", warnings[0])
            self.assertNotIn("ghost", fed.shards)
            self.assertIn("ghost", fed.missing)

    def test_auto_initializes_shard_when_no_db(self):
        root = os.path.join(self.tmp, "fresh")
        os.makedirs(root)
        add_root_to_config("fresh", root, self.cfg)
        with open_federation(self.cfg) as fed:
            self.assertIn("fresh", fed.shards)
            self.assertTrue(os.path.exists(imgdb.shard_db_path(root)))

    def test_partial_availability(self):
        root_ok = _make_root(self.tmp, "present", 1)
        add_root_to_config("present", root_ok, self.cfg)
        save_config(
            load_config(self.cfg)
            + [RootEntry(label="absent", abs_path=os.path.join(self.tmp, "gone"))],
            self.cfg,
        )
        with open_federation(self.cfg) as fed:
            self.assertIn("present", fed.shards)
            self.assertNotIn("absent", fed.shards)

    def test_asset_index_built_on_open(self):
        root = _make_root(self.tmp, "alpha", 3)
        add_root_to_config("alpha", root, self.cfg)
        with open_federation(self.cfg) as fed:
            scan_shard(fed, "alpha")
        # Reopen; index must be rebuilt from DB.
        with open_federation(self.cfg) as fed:
            alpha_ids = [aid for aid, lbl in fed.asset_index.items()
                         if lbl == "alpha"]
            self.assertEqual(len(alpha_ids), 3)


class AttachDetachTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "imgdb.conf")
        self.fed = open_federation(self.cfg)

    def tearDown(self):
        self.fed.close()
        shutil.rmtree(self.tmp)

    def test_attach_root_writes_config(self):
        root = _make_root(self.tmp, "newroot", 0)
        attach_root(self.fed, "newroot", root)
        entries = load_config(self.cfg)
        self.assertTrue(any(e.label == "newroot" for e in entries))

    def test_attach_root_initializes_shard_db(self):
        root = _make_root(self.tmp, "r", 0)
        attach_root(self.fed, "r", root)
        self.assertTrue(os.path.exists(imgdb.shard_db_path(root)))

    def test_detach_root_removes_from_config(self):
        root = _make_root(self.tmp, "r1", 0)
        attach_root(self.fed, "r1", root)
        detach_root(self.fed, "r1")
        self.assertFalse(any(e.label == "r1" for e in load_config(self.cfg)))

    def test_detach_nonexistent_raises(self):
        with self.assertRaises(RootNotFoundError):
            detach_root(self.fed, "nobody")


class ListRootsTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "imgdb.conf")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_list_roots_shows_ok_and_unavailable(self):
        root_ok = _make_root(self.tmp, "present", 1)
        add_root_to_config("present", root_ok, self.cfg)
        save_config(
            load_config(self.cfg)
            + [RootEntry(label="absent", abs_path=os.path.join(self.tmp, "gone"))],
            self.cfg,
        )
        with open_federation(self.cfg) as fed:
            roots = list_roots(fed)
        statuses = {label: status for label, _, status in roots}
        self.assertEqual(statuses["present"], "ok")
        self.assertNotEqual(statuses["absent"], "ok")

    def test_list_roots_order_matches_config(self):
        r1 = _make_root(self.tmp, "first", 0)
        r2 = _make_root(self.tmp, "second", 0)
        add_root_to_config("first", r1, self.cfg)
        add_root_to_config("second", r2, self.cfg)
        with open_federation(self.cfg) as fed:
            labels = [label for label, _, _ in list_roots(fed)]
        self.assertEqual(labels, ["first", "second"])


class RoutingTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "imgdb.conf")
        self.r1 = _make_root(self.tmp, "alpha", 2)
        self.r2 = _make_root(self.tmp, "beta", 2)
        add_root_to_config("alpha", self.r1, self.cfg)
        add_root_to_config("beta", self.r2, self.cfg)
        self.fed = open_federation(self.cfg)
        scan_shard(self.fed, "alpha")
        scan_shard(self.fed, "beta")

    def tearDown(self):
        self.fed.close()
        shutil.rmtree(self.tmp)

    def _alpha_id(self) -> str:
        return next(aid for aid, lbl in self.fed.asset_index.items()
                    if lbl == "alpha")

    def test_shard_for_asset_routes_to_correct_shard(self):
        aid = self._alpha_id()
        shard = shard_for_asset(self.fed, aid)
        self.assertEqual(shard.label, "alpha")

    def test_shard_for_unknown_asset_raises(self):
        with self.assertRaises(AssetNotFoundError):
            shard_for_asset(self.fed, "00000000-0000-0000-0000-000000000000")

    def test_shard_by_label_known(self):
        shard = shard_by_label(self.fed, "beta")
        self.assertEqual(shard.label, "beta")

    def test_shard_by_label_unknown_raises(self):
        with self.assertRaises(RootNotFoundError):
            shard_by_label(self.fed, "nobody")

    def test_scan_shard_populates_asset_index(self):
        # Verify scan_shard updates the index for newly discovered assets.
        new_root = _make_root(self.tmp, "gamma", 3)
        add_root_to_config("gamma", new_root, self.cfg)
        new_fed = open_federation(self.cfg)
        try:
            scan_shard(new_fed, "gamma")
            gamma_ids = [aid for aid, lbl in new_fed.asset_index.items()
                         if lbl == "gamma"]
            self.assertEqual(len(gamma_ids), 3)
        finally:
            new_fed.close()


class FederationWriteTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "imgdb.conf")
        self.r1 = _make_root(self.tmp, "alpha", 3)
        self.r2 = _make_root(self.tmp, "beta", 2)
        add_root_to_config("alpha", self.r1, self.cfg)
        add_root_to_config("beta", self.r2, self.cfg)
        self.fed = open_federation(self.cfg)
        scan_shard(self.fed, "alpha")
        scan_shard(self.fed, "beta")

    def tearDown(self):
        self.fed.close()
        shutil.rmtree(self.tmp)

    def _alpha_id(self) -> str:
        return next(aid for aid, lbl in self.fed.asset_index.items()
                    if lbl == "alpha")

    def _beta_id(self) -> str:
        return next(aid for aid, lbl in self.fed.asset_index.items()
                    if lbl == "beta")

    def _alpha_ids(self) -> list[str]:
        return [aid for aid, lbl in self.fed.asset_index.items()
                if lbl == "alpha"]

    def test_add_tags_routed_to_correct_shard(self):
        aid = self._alpha_id()
        add_tags(self.fed, aid, ["mytag"])
        shard = self.fed.shards["alpha"]
        count = shard.conn.execute(
            """SELECT COUNT(*) FROM asset_tags at
               JOIN tags t ON t.tag_id = at.tag_id
               WHERE at.asset_id = ? AND t.name = 'mytag'""",
            (aid,)
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_remove_tags_routed_to_correct_shard(self):
        aid = self._alpha_id()
        add_tags(self.fed, aid, ["label"])
        conn = self.fed.shards["alpha"].conn
        tag_id = conn.execute("SELECT tag_id FROM tags WHERE name = 'label'").fetchone()["tag_id"]
        remove_tags(self.fed, aid, [tag_id])
        count = conn.execute(
            "SELECT COUNT(*) FROM asset_tags WHERE asset_id = ?", (aid,)
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_set_caption_routed_to_correct_shard(self):
        aid = self._alpha_id()
        set_caption(self.fed, aid, "short", "hello")
        row = self.fed.shards["alpha"].conn.execute(
            "SELECT content FROM captions WHERE asset_id = ? AND kind = 'short'",
            (aid,)
        ).fetchone()
        self.assertEqual(row["content"], "hello")

    def test_delete_caption_routed_to_correct_shard(self):
        aid = self._alpha_id()
        set_caption(self.fed, aid, "short", "hello")
        delete_caption(self.fed, aid, "short")
        row = self.fed.shards["alpha"].conn.execute(
            "SELECT content FROM captions WHERE asset_id = ? AND kind = 'short'",
            (aid,)
        ).fetchone()
        self.assertIsNone(row)

    def test_rename_asset_via_federation(self):
        aid = self._alpha_id()
        asset = imgdb.get_asset(self.fed.shards["alpha"].conn, aid)
        new_rel = "renamed/" + os.path.basename(asset.rel_path)
        rename_asset(self.fed, aid, new_rel)
        updated = imgdb.get_asset(self.fed.shards["alpha"].conn, aid)
        self.assertEqual(updated.rel_path, new_rel)

    def test_delete_asset_removes_from_index(self):
        aid = self._alpha_id()
        delete_asset(self.fed, aid)
        self.assertNotIn(aid, self.fed.asset_index)

    def test_merge_same_shard_succeeds(self):
        ids = self._alpha_ids()
        self.assertGreaterEqual(len(ids), 2)
        survivor, merged = ids[0], ids[1]
        merge_assets(self.fed, survivor, merged)
        # merged id must still resolve via the index
        self.assertIn(merged, self.fed.asset_index)

    def test_merge_cross_shard_rejected(self):
        with self.assertRaises(CrossShardOperationError):
            merge_assets(self.fed, self._alpha_id(), self._beta_id())


class FederationFTSTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "imgdb.conf")
        self.r1 = _make_root(self.tmp, "alpha", 1)
        self.r2 = _make_root(self.tmp, "beta", 1)
        add_root_to_config("alpha", self.r1, self.cfg)
        add_root_to_config("beta", self.r2, self.cfg)
        self.fed = open_federation(self.cfg)
        scan_shard(self.fed, "alpha")
        scan_shard(self.fed, "beta")

    def tearDown(self):
        self.fed.close()
        shutil.rmtree(self.tmp)

    def _id_for(self, label: str) -> str:
        return next(aid for aid, lbl in self.fed.asset_index.items()
                    if lbl == label)

    def test_fts_fans_out_across_shards(self):
        set_caption(self.fed, self._id_for("alpha"), "short", "golden sunset")
        set_caption(self.fed, self._id_for("beta"), "short", "golden hour photo")
        results = search_captions(self.fed, "golden")
        self.assertEqual(len(results), 2)
        self.assertEqual({r[0] for r in results}, {"alpha", "beta"})

    def test_fts_no_match_returns_empty(self):
        self.assertEqual(search_captions(self.fed, "xyznomatch"), [])

    def test_fts_result_tuple_structure(self):
        aid = self._id_for("alpha")
        set_caption(self.fed, aid, "long", "mountain sunrise scene")
        results = search_captions(self.fed, "mountain")
        self.assertEqual(len(results), 1)
        label, result_aid, kind, content = results[0]
        self.assertEqual(label, "alpha")
        self.assertEqual(result_aid, aid)
        self.assertEqual(kind, "long")


class UserQueryTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "imgdb.conf")
        self.r1 = _make_root(self.tmp, "alpha", 2)
        add_root_to_config("alpha", self.r1, self.cfg)
        # Scan then reopen so the read connection sees the data.
        with open_federation(self.cfg) as fed:
            scan_shard(fed, "alpha")
        self.fed = open_federation(self.cfg)

    def tearDown(self):
        self.fed.close()
        shutil.rmtree(self.tmp)

    def test_query_returns_rows(self):
        _, rows = run_user_query(self.fed, "SELECT asset_id FROM all_assets")
        self.assertEqual(len(rows), 2)

    def test_query_returns_column_names(self):
        cols, _ = run_user_query(
            self.fed, "SELECT asset_id, rel_path FROM all_assets"
        )
        self.assertIn("asset_id", cols)
        self.assertIn("rel_path", cols)

    def test_query_no_shards_raises(self):
        with open_federation(os.path.join(self.tmp, "empty.conf")) as empty_fed:
            with self.assertRaises(FederationError):
                run_user_query(empty_fed, "SELECT 1")

    def test_query_all_assets_view_includes_root_column(self):
        cols, rows = run_user_query(
            self.fed, "SELECT _root, asset_id FROM all_assets"
        )
        self.assertIn("_root", cols)
        self.assertTrue(all(r[0] == "alpha" for r in rows))


if __name__ == "__main__":
    unittest.main(verbosity=2)
