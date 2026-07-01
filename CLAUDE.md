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

imgdb_ui.py       Main window. Imports from all layers above but adds no
                  business logic.

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

## Key patterns

**Transactions:** always use `imgdb.transaction(conn)` (an `@contextmanager` that issues `BEGIN IMMEDIATE` and rolls back on exception). Connections are opened with `isolation_level=None` (autocommit) so explicit `BEGIN` and sqlite3's implicit transaction control don't collide.

**Asset routing in the federation:** `federation.shard_for_asset(fed, asset_id)` looks up the shard via the in-memory index. The index covers both live and historically-merged IDs.

**Read connection:** `fed.read_conn` has `PRAGMA query_only = ON` and all shards ATTACH-ed. The `all_*` views are temporary and exist only for the life of the process. FTS5 tables cannot be unioned via views — caption search fans out per-shard in Python (`federation.search_captions`).

**Worker thread ownership:** `DBWorker` and `ThumbnailWorker` create their SQLite connections inside the worker thread. Never pass connections across threads.

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

**Multi-select dataset dialog:** `AddToDatasetDialog` (in `ui_dialogs.py`) presents a "New:" text field and a checkable `QListWidget` of existing datasets. `dataset_names() -> list[str]` returns all selected names (new + checked existing). A backward-compatible `dataset_name() -> str` accessor returns the first name. The OK button is disabled until at least one name is provided.

**Filter panel SQL entry:** `ui_filter_panel.py` has a dedicated `QPlainTextEdit` for raw SQL WHERE clauses (separate from the field-selector dropdown; `"sql"` is excluded from that dropdown). Clicking `+` appends a `FilterRule(field_id="sql", op="sql", value=<clause>)` to the active list. SQL rules render as monospace label + ✕ with no dropdowns.
