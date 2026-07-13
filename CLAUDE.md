# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Run all tests from `src/`:
```bash
cd src && python -m unittest discover -p "test_*.py"
```

Run a single test module:
```bash
cd src && python -m unittest test_imgdb
```

Run the GUI:
```bash
cd src && python imgdb_ui.py
```

## Dependencies

- Python 3.10+
- `pip install blake3 Pillow PySide6 numpy`

## Architecture

The codebase is split into strict layers — never import upward:

```
imgdb.py          Per-shard library. Single SQLite database. No knowledge of
                  other shards, federation, UI, or CLI.

federation.py     Federation layer. Opens all attached shards, builds an
                  in-memory asset_id→shard index, creates temporary union
                  views (all_assets, all_captions, all_tags, etc.) on a
                  read-only connection, and routes writes to exactly one shard.

filter_model.py   Pure data layer for the filter/sort system. No Qt, no
                  federation, no SQLite. Defines FilterField, SortField,
                  FilterRule, SortRule, and SQL fragment builders
                  (build_filter_conditions, build_sort_clause). All queries
                  that need dynamic WHERE/ORDER BY clauses import from here.

imgdb_thumbs.py   Toolkit-agnostic thumbnail worker. Single worker thread,
                  two priority levels (VISIBLE / BACKGROUND), no Qt imports.

imgdb_worker.py   Toolkit-agnostic DB worker. Wraps a Federation in a single
                  background thread with a FIFO job queue. SQLite connections
                  are created on the worker thread and never crossed.

imgdb_worker_qt.py / imgdb_thumbs_qt.py
                  Qt adapters only. Marshal callbacks from worker threads to
                  Qt signals on the main thread. No business logic.

ui_controller.py  AppController — widget-free QObject owning DB-write
                  submissions through the bridge. Exposes intent methods
                  (add_tag, save_caption, rename_asset, …) and emits change
                  signals (error, tag_suggestions_stale, tags_changed,
                  assets_changed, datasets_changed). No widget imports.

ui_flows.py       Multi-step UI flows that interleave worker jobs with dialogs
                  (CaptionImportFlow). State on the instance, one method per step.

imgdb_ui.py       Main window. Constructs widgets, wires panel signals →
                  controller/flow intents and controller signals → refresh
                  slots, and keeps refresh policy. No SQL, minimal logic.

ui_*.py           Individual UI panels and dialogs (asset table, detail panel,
                  roots panel, filter panel, query tab, dialogs, preview window).
```

`imgdb.conf` (INI format) maps root labels to absolute paths. It lives next to the entry point — not in `~/.config` or anywhere else.

## Design invariants (enforce these)

1. **Physical compartmentalization.** Each root owns `<root>/imgdb.sqlite` and `<root>/imgdb_thumbs/`. Nothing goes outside the root.
2. **No hidden files.** No dotfiles, no `~/.cache` entries, no tmp files that outlive a command.
3. **One file = one asset.** No automatic deduplication.
4. **Stable `asset_id`.** UUID that never changes. `current_hash` is mutable.
5. **Atomic disk + DB.** Disk ops are staged to a sibling `.imgdb-tmp-<uuid>` file, then DB is committed, then finalized. Rollback restores staging.
6. **No silent fallbacks.** Missing deps error at import. Conflicting label→path rebind errors.
7. **Cross-shard writes are forbidden.** `merge` requires both assets in the same shard.

## UI design principles (enforce these)

A refresh must never discard user context. Any operation that reloads or rebuilds a view — adding/removing tags, deleting assets, dataset changes — preserves what the user had before it ran: focus, scroll position, and selection. When adding a new UI flow, check it against each principle below; when fixing a violation, fix the shared helper and audit every call site with the same shape.

1. **Focus continuity.** After an action submitted from a text input (tag, category, caption, dataset name), focus ends up in the input the user would type into next — never dropped on a button or nowhere. Two established mechanisms in `ui_detail_panel.py`:
   - *Widget reuse:* `_TagsSection.load_tags` and `_TagGroup.load` reuse existing widgets across reloads (groups by type, chips by diff), so a reused input keeps focus for free. Prefer reuse over rebuild.
   - *Deferred focus for new widgets:* when the reload itself creates the widget (async DB round-trip), record the intent before emitting (`_TagsSection._pending_focus_type`) and apply focus when the rebuild lands.
2. **Scroll and selection preservation.** When rows disappear or the table refreshes in place (deletion, removal from the selected dataset, tag/caption edits), the refresh goes through `_apply_filter_preserving_scroll` and `_auto_select_after_refresh` in `imgdb_ui.py` — controller intents signal this via `assets_changed(preserve_scroll=True)`. Plain `_apply_filter` resets the viewport and is only for genuinely new result sets (changed filter rules).
3. **Defer past Qt's follow-up passes.** Focus and scrollbar writes issued during a model reset, reload, or dialog close get clobbered by Qt's subsequent layout pass (and `QCompleter`'s activated hook). Apply them via `QTimer.singleShot(0, ...)` after the completion signal — see `_apply_filter_preserving_scroll` and `_TagInput._submit`.

## Key patterns

**Transactions:** always use `imgdb.transaction(conn)` (an `@contextmanager` that issues `BEGIN IMMEDIATE` and rolls back on exception). Connections are opened with `isolation_level=None` (autocommit) so explicit `BEGIN` and sqlite3's implicit transaction control don't collide.

**Asset routing in the federation:** `federation.shard_for_asset(fed, asset_id)` looks up the shard via the in-memory index. The index covers both live and historically-merged IDs.

**Read connection:** `fed.read_conn` has `PRAGMA query_only = ON` and all shards ATTACH-ed. The `all_*` views are temporary and exist only for the life of the process. FTS5 tables cannot be unioned via views — caption search fans out per-shard in Python (`federation.search_captions`).

**Worker thread ownership:** `DBWorker` and `ThumbnailWorker` create their SQLite connections inside the worker thread. Never pass connections across threads.

**Compute worker (perceptual-hash backfill):** image decode+DCT must not run on the DB worker — it would block every UI read for the batch duration. `MainWindow` runs a second `DBWorker` started with a null factory (`lambda: None`, no federation) purely as a CPU thread. The backfill pipelines across three hops so reads stay responsive: DB worker fetches `(asset_id, label, abs_path)` → compute worker decodes → DB worker writes via `set_perceptual_hash`. Compute jobs only read files off disk; they never touch a SQLite connection.

**Logging:** worker modules acquire `logging.getLogger(__name__)` and log (never silently swallow) failed jobs and raising `on_result`/`on_error` callbacks, while keeping the worker loop alive. Only the entry point (`imgdb_ui.main`) configures a handler; library modules never add handlers.

**UI layering — no SQL in UI:** UI files contain no SQL strings. All data access goes through `imgdb`/`federation` accessors (e.g. `federation.find_asset_by_abs_path`, `imgdb.get_captions_for_asset`). Verify: `grep -rn 'execute(' src/ui_*.py src/imgdb_ui.py` returns nothing.

**Controller / refresh policy:** mutations go through `AppController` intents; refresh is driven by *which* signal the intent emits, not by per-handler `on_done` closures. `tag_suggestions_stale` → debounced suggestion rescan only (single tag edits); `tags_changed` → full tag refresh (management ops); `assets_changed(preserve_scroll)` → `_apply_filter[_preserving_scroll]`; `datasets_changed` → dataset list. `MainWindow` connects these once in `_connect_signals`. A few bespoke handlers (delete row-reselection, merge file removal, fetch-then-dialog dataset adds, bulk import) still orchestrate directly in `MainWindow`.

**Method naming (UI):** `refresh_*` = re-fetch from DB and update widgets; `load_*` = populate widgets from already-fetched data; `on_*`/`_on_*` = signal handlers.

**Write→read ordering contract:** detail-panel refresh correctness relies on direct (synchronous) signal connections: a mutation handler submits its write job to the FIFO `DBWorker` queue *before* the subsequent reload job is submitted (`ui_detail_panel.py`), so the reload always observes the write. Do not make these connections queued/async.

**Filter debounce:** `FilterPanel` routes value-edit keystrokes through a single-shot `QTimer` (`_emit_debounced`, 250ms) so a full model reset does not fire per keystroke. Structural changes (add/remove rule, dataset toggle, sort) call `_emit_now`, which emits immediately and cancels any pending keystroke. Tag-suggestion rescans are similarly debounced (~1s) in `MainWindow`.

**Correlated-subquery filter fields cost:** `filter_model` fields `tag_count`, `caption_count`, `duplicate_count`, `perceptual_duplicate_count` are correlated subqueries. Per-shard indexes serve each evaluation in O(shards × log n), but that is multiplied by every candidate row when used as a filter. Acceptable at current scale; if page-fetch timings (logged at `logger.debug` in `ui_asset_table._fetch_page`) show these dominate, rewrite as joins against grouped subqueries.

**Worker shutdown guard:** `DBWorker.is_running` (`self._thread is not None and self._thread.is_alive()`) and its forwarding property on `QtDBBridge` are used to prevent recursive job submission after shutdown. Background batch loops (e.g. perceptual hash backfill) check `self._bridge.is_running` before re-queuing the next batch.

**Tag validation:** Root labels must match `[A-Za-z_][A-Za-z0-9_]*` because they are interpolated as SQLite schema names in `ATTACH DATABASE ... AS <label>`. This is the only place SQL identifiers are interpolated; all data goes through parameterized queries.

**Perceptual hash sentinel:** `imgdb.PHASH_FAILED = ""` (empty string) is stored when `compute_perceptual_hash` returns `None` (unprocessable file). `NULL` in `perceptual_hash` means "not yet attempted". All backfill queries filter `WHERE perceptual_hash IS NULL`, which automatically excludes the sentinel so failed files are never re-queued. Duplicate-count subqueries must also exclude the sentinel (`AND perceptual_hash != ''`) to avoid false positives among all-empty-string rows.

**Asset table columns:** `ui_asset_table.py` defines columns 0–7:

| Index | Constant   | Header            | Default hidden |
|-------|-----------|-------------------|----------------|
| 0     | COL_THUMB  | (thumbnail)       | no             |
| 1     | COL_PATH   | Path              | no             |
| 2     | COL_ROOT   | Root              | no             |
| 3     | COL_DIMS   | Dimensions        | no             |
| 4     | COL_FORMAT | Format            | no             |
| 5     | COL_SIZE   | Size              | no             |
| 6     | COL_PHASH  | Perceptual Hash   | **yes**        |
| 7     | COL_ID     | Asset ID          | no             |

Column visibility is toggled via a right-click menu on the header. State persists via `QHeaderView.saveState()` / `restoreState()`. When restoring, defaults for hidden columns are applied before `restoreState` so that new columns (absent from an old saved state) start hidden.

**Context menu extensibility:** `AssetTableView` emits `context_menu_requested(menu: QMenu, assets: list[AssetRow])`. The view populates copy actions for the clicked cell; the parent (`imgdb_ui.py`) connects to this signal and appends a separator plus row-level actions. Pattern:

```python
def _on_asset_context_menu(self, menu, assets):
    if not assets or not menu:
        return
    if menu.actions():
        menu.addSeparator()
    act = menu.addAction("Some Action")
    act.triggered.connect(lambda: self._some_method(assets))
```

**Image attributes (EAV):** `image_attributes(asset_id, key, value)` is a vertical entity-attribute-value store — new per-image data becomes new rows, not new columns, so tracking another attribute never needs a schema migration (it's a brand-new table, so `CREATE TABLE IF NOT EXISTS` in `SCHEMA_SQL` adds it to existing shards on open). Booleans are presence-style: value `'1'` when true, row absent when false (`imgdb.set_image_flag`/`get_image_flag`); the `(key, value)` index serves filters. Filter fields (`filter_model`) are correlated subqueries over `all_image_attributes` — the same cost profile as `tag_count` et al. Current keys: `ATTR_IS_FAVORITE` (toggled from the detail panel), `ATTR_IS_LAST_SCAN` (set by `_mark_last_scan` in the scan's final transaction: clears the flag shard-wide, then flags exactly that scan's `new_ids`). Replicate this table per object type (datasets, roots) when needed — images only for now.

**Dataset surrogate keys & pins:** `datasets.dataset_id` is a UUID, backfilled by `_migrate` on every open (fills NULLs only — never regenerate, pins reference it) and stable across renames because `rename_dataset` UPDATEs the row. Pinned datasets are stored in `imgdb_ui.ini` (`datasets/pinned`, comma-joined) as UUIDs only — never names, so the centralized config leaks nothing about shard contents (invariant #1). A logical dataset is pinned if any per-shard UUID matches; `ui_pins.resolve_pins` self-heals by widening the set with sibling-shard UUIDs at display time. Pure logic in `ui_pins.py` (no Qt); persistence and rendering in `MainWindow` (`_resolve_pinned_names`, `_on_dataset_pin_toggled`).

**Multi-select dataset dialog:** `AddToDatasetDialog` (in `ui_dialogs.py`) presents a "New:" text field and a checkable `QListWidget` of existing datasets. `dataset_names() -> list[str]` returns all selected names (new + checked existing). A backward-compatible `dataset_name() -> str` accessor returns the first name. The OK button is disabled until at least one name is provided.

**Filter panel SQL entry:** `ui_filter_panel.py` has a dedicated `QPlainTextEdit` for raw SQL WHERE clauses (separate from the field-selector dropdown; `"sql"` is excluded from that dropdown). Clicking `+` appends a `FilterRule(field_id="sql", op="sql", value=<clause>)` to the active list. SQL rules render as monospace label + ✕ with no dropdowns.
