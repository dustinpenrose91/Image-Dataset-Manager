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
                  roots panel, query tab, dialogs, preview window).
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

**Tag validation:** Root labels must match `[A-Za-z_][A-Za-z0-9_]*` because they are interpolated as SQLite schema names in `ATTACH DATABASE ... AS <label>`. This is the only place SQL identifiers are interpolated; all data goes through parameterized queries.
