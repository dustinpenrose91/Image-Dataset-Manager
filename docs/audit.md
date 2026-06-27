# Codebase Audit

## Summary

The codebase is architecturally sound and the layering invariants are well-enforced throughout. The transaction discipline, worker-thread ownership, and thumbnail priority system are all correct. The main areas of concern fall into two buckets: a small number of real bugs (one type mismatch and one wrong SQL column name that would cause runtime failures), several encapsulation violations where the GUI layer pierces private state, and one significant performance issue where every page fetch materialises the entire result set before slicing. Low-priority findings are mostly dead code and minor hygiene issues.

---

## High Priority

### [federation.py:1404] Wrong column name `tag_type_id` in `list_all_tags_with_counts`

The `all_tag_types` union view exposes the schema column `type_id` (from `tag_types`), not `tag_type_id`. The JOIN condition `tt.tag_type_id = t.type_id` will always produce zero rows, making `list_all_tags_with_counts` silently return an empty list. This breaks the tag suggestion autocomplete in detail panel inputs whenever there are tags.

**Fix:** Change `tt.tag_type_id` to `tt.type_id` on line 1404.

---

### [imgdb_ui.py:210] Wrong argument passed to `RootsPanel`

`RootsPanel.__init__` expects `(bridge: QtDBBridge, config_path: str, ...)`. The call at line 210 passes `self._thumb_worker` (a `ThumbnailWorker`) as the second argument. Python does not error at construction time because `_config_path` is stored but never subsequently used in `ui_roots_panel.py`. The consequence is that `_config_path` silently holds the wrong object type, and any future use of it (or a type-check tool) will break.

The root cause is that `config_path` was added as a parameter to `RootsPanel` but is unused — likely a stale parameter from an earlier design. The `thumb_worker` argument was also removed from the public API at some point but not from the call site.

**Fix:** Remove the `config_path` parameter from `RootsPanel.__init__` entirely (it is stored but never read). Update the call in `imgdb_ui.py:210` to `RootsPanel(self._bridge)`.

---

### [ui_asset_table.py:305-320] Every page fetch streams the entire result set

`_fetch_page` calls `list(federation.list_filtered_assets(...))` — materialising all N rows from the DB — and then returns `rows[offset:offset+PAGE_SIZE]`. For a federation with 100,000 assets, fetching page 10 allocates 100,000 `AssetRow` objects and discards 99,800 of them. The comment acknowledges the issue but frames it as a known limitation. It is actually fixable without an API change: the generator can be consumed up to `offset + PAGE_SIZE` with `itertools.islice`, avoiding the full allocation.

**Fix:** Replace the `list(...)` + slice with:
```python
from itertools import islice
rows = list(islice(
    federation.list_filtered_assets(fed, ...),
    offset + PAGE_SIZE
))[offset:]
```
This streams only `offset + PAGE_SIZE` rows and discards nothing unnecessarily. A proper cursor-based offset in `list_filtered_assets` would be the ideal long-term fix but this is a low-friction improvement.

---

## Medium Priority

### [imgdb_ui.py:348] Direct mutation of `DBWorker._fed`

`_on_roots_changed` submits a job that replaces the federation with `self._worker._fed = new_fed`. This bypasses the encapsulation of `DBWorker` by directly setting a private attribute from within the job closure (which runs on the worker thread, so the thread ownership is at least correct). The right approach is to add a `DBWorker.replace_federation(new_fed)` method or restructure so the worker's `_run` loop handles federation replacement.

**Fix:** Add a `DBWorker.set_federation(new_fed: Federation)` method and call it from the job closure rather than directly mutating `_worker._fed`.

---

### [ui_asset_table.py:403-404] GUI thread reads `_bridge._worker._fed` without synchronisation

`_find_existing_lq_thumb` accesses `self._bridge._worker._fed` from the GUI thread (inside `data()`, which is called during paint). The `Federation` object is owned by the worker thread. Reading `fed.shards.get(...)` from the GUI thread while the worker might be modifying the federation (during `_on_roots_changed`) is a data race. In CPython the GIL provides practical protection for simple dict lookups, but this is not guaranteed and is fragile by design.

Additionally this method pierces two layers of encapsulation: `_bridge._worker` (QtDBBridge's private worker) and `_worker._fed` (DBWorker's private federation).

**Fix:** Expose `abs_path` for a root label through a thread-safe accessor on the bridge (e.g. `QtDBBridge.root_abs_path(label) -> Optional[str]` that reads a snapshot) rather than reading the live federation object.

---

### [ui_roots_panel.py:394] `RootsPanel` reads `RootEntry._abs_path` directly

`_on_delete_requested` accesses `entry._abs_path` (a private attribute of `RootEntry`). `RootEntry` already exposes `is_checked()` as a public accessor; `abs_path` should get the same treatment.

**Fix:** Add a `RootEntry.abs_path(self) -> str` property and use it.

---

### [ui_preview_window.py:546] `PreviewWindow` mutates `_Canvas._crop_rect` directly via `setattr`

The cancel-crop lambda uses `setattr(self._canvas, '_crop_rect', None)`. This bypasses `_Canvas`'s internal consistency — the crop state should be cleared through an existing or new method.

**Fix:** Add a `_Canvas.clear_crop()` method and call it from the cancel lambda.

---

### [ui_detail_panel.py:519-545] Inline SQL in detail panel bypasses federation layer

`_load_tags_and_captions` executes raw SQL directly against `shard.conn` (tag query, caption query, dataset membership query) rather than going through `imgdb` or `federation` functions. This is a layer violation: `ui_detail_panel.py` knows about the schema and connection objects of the DB layer, which is `imgdb.py`'s domain.

**Fix:** Add federation-level functions (or `imgdb`-level functions) for `get_tags_for_asset(conn, asset_id)` and `get_dataset_membership(conn, asset_id)`. The caption fetch is already partly available via `imgdb.search_captions` patterns.

---

### [federation.py:1576] `prescan_ambiguous_matches` calls `shard_for_asset` unnecessarily per asset

`AssetRow` already carries `asset.root`. `shard_for_asset` does a dict lookup on `fed.asset_index` then `fed.shards` — two dicts — when `fed.shards.get(asset.root)` is all that is needed. Same issue exists in `bulk_import_caption_tags` line 1607 via `import_caption_tags_for_asset`.

**Fix:** Replace `shard = shard_for_asset(fed, asset.asset_id)` with `shard = fed.shards.get(asset.root)` (with a None check) in both functions.

---

### [imgdb_ui.py:851-854] `_batch_add_tag` issues one federation call per asset

When adding tags to N selected assets, the worker job loops calling `federation.add_tags` once per asset. `federation.add_tag_to_asset_ids` already exists and handles the exact same operation in batches grouped by shard. The per-asset loop N-multiplies the transaction overhead.

**Fix:** Replace the loop with `federation.add_tag_to_asset_ids(fed, asset_ids, tags[0], type_name)` — or if multiple tags are needed, call it once per tag.

---

## Low Priority

### [ui_roots_panel.py:272] Tautological expression always evaluates to `True`

```python
checked = label not in self._checked_labels or label in self._checked_labels
```
`(A or not-A)` is always `True`. This `checked` value is immediately overwritten in the `if` branch below, so it has no effect, but it is confusing dead code.

**Fix:** Remove the line entirely. The variable is reassigned unconditionally before use.

---

### [federation.py:33 and 519] `shutil` imported twice

`shutil` is imported at module level (line 33) and again with `import shutil` inside `delete_root` (line 519). The inner import is a no-op but is misleading.

**Fix:** Remove the local `import shutil` inside `delete_root`.

---

### [ui_roots_panel.py — late import] `QDialog` and `imgdb` imported at module bottom

Lines 462-464 import `QDialog` and `imgdb` at the bottom of `ui_roots_panel.py` with a comment about avoiding circular imports. If a circular import actually exists it should be resolved structurally; if it does not exist, move the imports to the top. Deferred imports at module level are surprising and make dependency tracking harder.

**Fix:** Verify whether the circular import still exists and either resolve it or move the imports to the top of the file.

---

### [imgdb_ui.py:425] `QItemSelectionModel` imported inside a method

`from PySide6.QtCore import QItemSelectionModel` appears inside `_auto_select_after_refresh`. PySide6 is already imported at the module level extensively; this symbol should be added to the module-level import block.

**Fix:** Move to the top-level `from PySide6.QtCore import ...` import.

---

### [imgdb_ui.py:609-610, 647-649] `AmbiguousTagResolutionDialog` and `ImportFromCaptionDialog` imported inside a callback

These are imported inside `on_fetch` (a closure). They should be at the top of the module with the other dialog imports. The deferred import pattern is used elsewhere in the codebase for circular-import avoidance (which does not apply here — both are in `ui_dialogs`).

**Fix:** Move to the existing `from ui_dialogs import ...` block at the top of `imgdb_ui.py`.

---

### [ui_preview_window.py — undocumented dependency on numpy]

`ui_preview_window.py` imports `numpy` for mask array operations. `numpy` is not listed in the `CLAUDE.md` dependencies section (`pip install blake3 Pillow PySide6`). A fresh install will fail at mask-related functionality with an `ImportError`.

**Fix:** Add `numpy` to the documented dependencies in `CLAUDE.md` and enforce it with the same guard-at-import pattern used for `blake3` and `Pillow`.

---

## Non-issues (patterns that look suspicious but are intentional)

- **`list_filtered_assets` streams with `fetchmany(1000)`** — correct; the generator pattern avoids holding the full result in memory on the federation side. The perf issue is in the caller (`ui_asset_table.py`), not here.
- **`conn.executescript(SCHEMA_SQL)` in `init_shard`** — safe because all DDL uses `IF NOT EXISTS`; idempotent schema migrations are by design.
- **`isolation_level=None` (autocommit) on all connections** — intentional per documented transaction discipline; `transaction()` issues explicit `BEGIN IMMEDIATE`.
- **`_worker._fed = new_fed` runs on the worker thread** — the encapsulation violation is flagged above, but the thread-ownership aspect is correct: the mutation happens inside a submitted job, so it executes on the worker thread that owns the federation.
- **`_ensure_relay_class()` lazily constructs the `Relay` class** — required because PySide6 metaclass machinery cannot run at import time in non-Qt environments (tests, CLI).
- **`queue_depth()` O(N) over the heap** — acceptable because the heap is bounded by the viewport size plus background batch count, not total asset count.
- **`_find_existing_lq_thumb` uses `os.listdir`** — the directory contains at most a small number of files per fan-out bucket (fan is first two hex chars of UUID, so O(1) expected files per bucket for typical collections), making this acceptably fast.
