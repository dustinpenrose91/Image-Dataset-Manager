# Code Audit Report — Image Dataset Manager

## Context

Full audit of the federated image-dataset catalog (~13.2k lines, Python/PySide6, SQLite shards) requested ahead of a growth phase: ~100k images today across several shards, expected to grow. Priorities, in order: (1) engineering best practices, scalability, efficiency, bugs/points of failure; (2) UI streamlining — cleaner, snappier, less spaghetti (user chose **aggressive refactor** scope); (3) CLI — user confirmed the documented CLI does not exist and docs should be corrected.

Baseline: all 161 tests pass in ~5s. Working tree clean at commit `801a23d`. The June 26 audit (`docs/audit.md`) findings are all fixed. Architecture layering (imgdb → federation → workers → Qt adapters → UI) is genuinely enforced with a handful of violations noted below.

This report is the implementation plan. Each item has file:line anchors, the defect, and the suggested change. Work through phases in order; run `cd src && python -m unittest discover -p "test_*.py"` after each phase.

---

## Phase 1 — P0 Bug (fix first, with regression test)

### 1.1 `prescan_ambiguous_matches` is broken — bulk "Import from caption" with "Ask per tag" always crashes

- **Where:** `federation.py:1552-1585`; caller `imgdb_ui.py:803-816`.
- **Defect (verified):** The function signature still has the pre-refactor params (`where_clause`, `show_missing`, `dataset_name`, `tag_filter`) and forwards them to `list_filtered_assets` (`federation.py:1568-1575`), whose signature is now `(fed, checked_labels, filter_rules, sort_rules, limit, offset)` (`federation.py:914-921`). The UI passes `filter_rules=rules` in `bulk_kwargs` (`imgdb_ui.py:771-776`). Every invocation raises `TypeError` — filtered scope fails at the call site, "all" scope fails inside at the `list_filtered_assets` call.
- **Fix:** Change the signature to mirror `bulk_import_caption_tags` (`federation.py:1588-1595`): `(fed, caption_kind, tag_lookup, checked_labels=None, filter_rules=None)`. Body calls `list_filtered_assets(fed, checked_labels, filter_rules or [], [])`.
- **Test:** Add to `test_federation_ops.py`: seed two tags with the same name in different types, a caption containing that name, call `prescan_ambiguous_matches` with and without `filter_rules` — assert it returns the ambiguous name instead of raising.

---

## Phase 2 — Robustness / points of failure

### 2.1 Silent job and callback failures in both workers

- **Where:** `imgdb_worker.py:284-302` (`DBWorker._execute`), `imgdb_thumbs.py:332-346` (thumbnail equivalent).
- **Defect:** A job that raises with `on_error=None` vanishes without a trace. Exceptions raised *by* `on_result`/`on_error` callbacks are swallowed with bare `pass`. No logging anywhere in the codebase.
- **Fix:** Add `logging.getLogger(__name__)` to both worker modules; replace each bare `pass`/silent-return with `logger.exception(...)` including the job's function name. Keep the "never kill the worker loop" behavior. Configure a basic stderr handler in `imgdb_ui.py:main` (and nowhere else — libraries only get loggers, not handlers).

### 2.2 Failed thumbnail lookup leaves a permanently blank cell

- **Where:** `ui_asset_table.py:340-373`.
- **Defect:** `fetch` returns `None` on any error (`ui_asset_table.py:352-353`) and `on_ready_data` returns early, but `aid` stays in `_thumb_requested` forever — that row never retries and shows no thumbnail until the next full refresh. The `bridge.submit` at line 372 also passes no `on_error`.
- **Fix:** In `on_ready_data`, when `data is None`, do `self._thumb_requested.discard(aid)` (allows retry on next paint) and log. Pass an `on_error` that does the same.

### 2.3 `_on_thumb_ready` linear scan — O(rows) per thumbnail event

- **Where:** `ui_asset_table.py:405-410`.
- **Defect:** Every arriving thumbnail scans `self._rows` (up to full result-set size) to find its row. During scroll bursts at 100k rows this is millions of comparisons per second on the GUI thread.
- **Fix:** Maintain `self._row_by_id: dict[str, int]` updated in `_fetch_page.on_result` and cleared in `refresh()`. `_on_thumb_ready` becomes a dict lookup + bounds check (`i < len(self._rows)`).

### 2.4 Orphaned `.imgdb-tmp-*` staging files after crash

- **Where:** `imgdb.py` rename/delete staging (staging at `imgdb.py:1100`, `imgdb.py:1142`).
- **Defect:** Crash between DB commit and finalize orphans the staging file; nothing ever reports or cleans them. Accumulates silently — violates the spirit of invariant #2 (no tmp files that outlive a command).
- **Fix (surface, don't silently delete — invariant #6):** During `scan_root`, emit an `on_event("stale_staging", rel_path)` for any `.imgdb-tmp-*` file encountered; UI shows them in the scan summary. Deletion stays a user action.

---

## Phase 3 — Performance & scalability (100k → growth)

### 3.1 No debounce on filter input — full model reset per keystroke

- **Where:** `ui_filter_panel.py:113` (`textChanged → changed`), chained via `ui_filter_panel.py:459,498` to `filter_changed`, connected at `imgdb_ui.py:244` to `_apply_filter` (`imgdb_ui.py:365-376`), which calls `AssetTableModel.refresh()` → `beginResetModel` + COUNT query + page fetch.
- **Fix:** Debounce in `FilterPanel`: route all `filter_changed.emit()` calls through a single-shot `QTimer` (~250ms). Structural changes (rule added/removed, checkbox toggles) may bypass the timer; text edits must not. Keep `_apply_filter` unchanged.

### 3.2 Perceptual-hash backfill runs image decoding on the DB worker thread

- **Where:** `imgdb_ui.py:653-698` (`_run_phash_batch`); `imgdb.compute_perceptual_hash` called at `imgdb_ui.py:671` inside a DB-worker job.
- **Defect:** Pillow decode+DCT per image executes on the single serialized DB worker, blocking every UI read (page fetches, counts, detail loads) for the duration of a batch. The ini currently has `batch_size=1` — evidence the user had to throttle it to keep the UI usable, making backfill crawl.
- **Fix:** Split compute from write. Run hashing on its own thread (reuse the `ThumbnailWorker` single-thread + priority pattern from `imgdb_thumbs.py` — or generalize that worker to accept a compute callable). Pipeline per batch: DB worker fetches `(asset_id, abs_path)` list → compute thread hashes → DB worker writes results via `imgdb.set_perceptual_hash`. Keep the `PHASH_FAILED` sentinel logic (`imgdb_ui.py:672-677`) and the `is_running` re-queue guard (`imgdb_ui.py:685`). Default batch size can then go to ~50 without UI impact.

### 3.3 Every tag add/remove fires 3 worker jobs including a global tag rescan

- **Where:** `imgdb_ui.py:551-561` (`_add_tag`/`_remove_tag` → `_refresh_tag_suggestions` = `list_all_tags_with_counts`, a full scan of every shard's tags), plus `ui_detail_panel.py:583-591` (full 5-query detail reload).
- **Fix:**
  - Debounce `_refresh_tag_suggestions` with a single-shot QTimer (~1s) so bursts of tag edits coalesce into one rescan.
  - In the detail panel, make the post-mutation reload fetch only tags+validation state, not the full 5-piece bundle (split `_load_tags_and_captions` at `ui_detail_panel.py:535` into `_load_tags` / `_load_captions` / `_load_datasets`; mutation handlers call only the relevant one).

### 3.4 OFFSET pagination re-sorts the whole filtered set per page

- **Where:** `federation.py:944-954` (`LIMIT ? OFFSET ?` with `ORDER BY` over the `all_assets` UNION ALL view).
- **Defect:** No index can serve an ORDER BY across a union view, so SQLite sorts the entire filtered set for each page fetch — O(N log N) per page, per scroll into unloaded territory. Works at 100k; degrades linearly with growth.
- **Fix (measured, not speculative):** First add timing via `logger.debug` around page fetches. If page fetches exceed ~100ms at current scale, implement filter-epoch ID caching: on each `refresh()`, one worker job materializes the sorted `asset_id` list for the current filter (IDs only — ~100k × 36 bytes is fine in memory), and page fetches become `WHERE asset_id IN (…page ids…)` + reorder in Python. This also makes COUNT free and removes the double reset in 3.5.
- **Note:** Do NOT add indexes for `rel_path` or `asset_tags(asset_id)` — UNIQUE and PK constraints already create them (see Non-issues).

### 3.5 Double model reset per filter change

- **Where:** `ui_asset_table.py:127-145` (`refresh` resets) then `ui_asset_table.py:234-241` (`_fetch_count.on_result` resets again).
- **Fix:** Cheap cleanup — folds into 3.4's epoch cache if implemented; otherwise leave the first reset (it clears the view during load) and accept it. Low priority.

### 3.6 Correlated-subquery filter fields

- **Where:** `filter_model.py:72-88` (`tag_count`, `caption_count`, `duplicate_count`, `perceptual_duplicate_count`).
- **Assessment:** Predicate pushdown lets per-shard indexes (`idx_assets_hash`, `idx_assets_phash`, `idx_captions_asset`, `asset_tags` PK) serve these, so each evaluation is O(shards × log n) — tolerable but multiplied by every candidate row when used as a filter. If profiling (3.4's timing) shows these dominate, rewrite as joins against grouped subqueries, e.g. `JOIN (SELECT file_hash, COUNT(*) c FROM all_assets GROUP BY file_hash)`. Otherwise document the cost in CLAUDE.md and move on.

---

## Phase 4 — UI refactor (aggressive scope, per user)

Do this as its own commit series after Phases 1–3 land, so behavior changes stay bisectable.

### 4.1 Extract a controller layer from `imgdb_ui.py`

- **Problem:** `MainWindow` (1524 lines) is wiring + business logic + refresh policy. Signal chains run panel → main window handler → bridge closure → nested result callbacks (e.g. `_connect_signals` block at `imgdb_ui.py:244-312`).
- **Change:** New module `ui_controller.py` — a plain (non-widget) `QObject` owning `QtDBBridge` submissions and refresh policy. It exposes intent methods (`add_tag(asset_id, name, type)`, `save_caption(...)`, `import_from_caption(...)`, `run_phash_backfill()`) and result signals (`tags_changed`, `captions_changed`, `assets_changed`, `error(str)`). `MainWindow` shrinks to: construct widgets, connect panel signals → controller methods, controller signals → panel refresh slots. Move `_add_tag`, `_remove_tag`, `_save_caption`, `_delete_caption`, `_set_tags_validated`, `_set_caption_validated`, `_on_mask_saved`, `_on_perceptual_hash_ready`, `_batch_*`, `_tm_*` handler bodies (`imgdb_ui.py:551-706`, `~1150-1300`) into it.
- **Constraint:** Controller imports federation/workers but no widgets; panels import neither federation nor the bridge (today `ui_asset_table.py` and `ui_detail_panel.py` submit bridge jobs directly — route those through the controller too, or at minimum through injected callables).

### 4.2 Extract the caption-import flow

- **Where:** `imgdb_ui.py:707-820` — 115 lines of nested closures spanning fetch → dialog → single/bulk → prescan → resolve → import.
- **Change:** New `CaptionImportFlow` class in `ui_controller.py` (or `ui_flows.py`): each step a named method, state (scope, kwargs, resolution) as instance attributes instead of closure captures. The prescan fix (1.1) makes this testable end-to-end.

### 4.3 Move raw SQL out of UI callbacks

- **Where (layering violations — CLAUDE.md claims data goes through imgdb/federation):** `imgdb_ui.py:714-717` (caption fetch), `imgdb_ui.py:591-596` (rel_path lookup in `_on_mask_saved`), `imgdb_ui.py:602+` (same pattern in `_on_perceptual_hash_ready`), `ui_detail_panel.py:535+` (`_load_tags_and_captions` bundle).
- **Change:** Add the missing accessors: `imgdb.get_captions_for_asset(conn, asset_id)`, `imgdb.get_asset_by_rel_path(conn, rel_path)`, `federation.find_asset_by_abs_path(fed, abs_path)` (subsumes the shard-loop in both mask/phash handlers), `federation.get_asset_detail(fed, asset_id)` returning the detail-panel bundle. UI files then contain zero SQL strings — enforce with a grep check in the docs (see 5.3).

### 4.4 Model/view rewrites

- **Tag management panel** (`ui_tag_panel.py:46-57`): replace `QTableWidget` with `QTableView` + a small `QAbstractTableModel` (mirror the pattern in `ui_asset_table.py`). Enables incremental updates instead of clear-and-repopulate.
- **Detail-panel tag chips** (`ui_detail_panel.py:161-170`): `load()` destroys and recreates every chip widget per update. Diff instead: keep `dict[tag_id, chip]`, add/remove only deltas.
- **Caption blocks** (`ui_detail_panel.py:568-581`): same — update the changed block in place, keyed by `kind`; full rebuild only when the kind set changes.

### 4.5 Deduplicate handler pairs and standardize conventions

- Merge `_tm_add_to_filtered` / `_tm_remove_from_filtered` (`imgdb_ui.py:~1220-1252`) into one parameterized method; audit for other near-identical pairs while moving handlers into the controller (4.1).
- Naming convention, then apply mechanically: `refresh_*` = re-fetch from DB and update UI; `load_*` = populate widgets from already-fetched data; `on_*` = signal handlers. Document in CLAUDE.md.
- Signal connections: all panel→controller wiring lives in one `MainWindow._connect_signals`, no inline `.connect` scattered through `_build_ui`.

---

## Phase 5 — Documentation & tests

### 5.1 README: strip the CLI (user decision)

- `docs/README.md:63` (architecture entry), `:78-88` (running section), `:158-269` (entire CLI commands section incl. query/fts examples). Remove; keep the schema-overview and example-SQL content by moving useful queries into the Query-tab section. Also remove the CLAUDE.md sentence "There is a CLI as well" if present (it isn't — but scan for CLI references: `imgdb.py:6` docstring mentions argparse only as a non-dependency, fine).
- Preserve the user's joke annotation at `docs/README.md:138` untouched.

### 5.2 Delete `docs/audit.md`

All findings fixed; replace with this report (or delete once implemented — the user's standards favor deletion over stale docs).

### 5.3 CLAUDE.md additions (keep token-lean)

- **Write→read ordering contract:** detail-panel refresh correctness depends on: direct signal connections are synchronous, so the mutation job is submitted to the FIFO DBWorker queue *before* the reload job (`ui_detail_panel.py:583-586`). One paragraph under Key patterns.
- **UI layering rule:** "UI files contain no SQL. Verify: `grep -rn 'execute(' src/ui_*.py src/imgdb_ui.py` returns nothing." (true after 4.3).
- **Controller layer:** one architecture-diagram line for `ui_controller.py` once 4.1 lands.
- **Correlated filter fields cost note** (from 3.6) if not rewritten.
- Update the worker section if 3.2 adds a compute thread.

### 5.4 Test gaps to fill (unittest + PySide6's bundled QtTest — no new dependencies)

Priority order:
1. Prescan regression (Phase 1) — `test_federation_ops.py`.
2. Atomic rollback paths: force a failure between staging and finalize in `rename_asset`/`delete_asset`; assert file restored and DB unchanged — `test_imgdb.py`.
3. `scan_root_init` → `scan_root_batch` → `scan_root_finish` incremental flow — `test_imgdb.py`.
4. `set_perceptual_hash` + `PHASH_FAILED` sentinel: failed files excluded from `list_assets_missing_perceptual_hash` — `test_perceptual_hash.py`.
5. Migration `v1_add_tag_types`: build a pre-migration schema, run `apply_migrations`, assert tags preserved as General — new `test_migrations.py`.
6. `AssetTableModel` paging + thumb-retry (2.2, 2.3) with a stubbed bridge — new `test_ui_asset_table.py` (model only; no widgets needed).
7. Controller-level tests for `CaptionImportFlow` once 4.2 exists.

---

## Verified non-issues — do not "fix" these

- **No index on `assets(rel_path)` / `asset_tags(asset_id)`:** `UNIQUE` and composite PRIMARY KEY create backing indexes automatically; `asset_tags` PK `(asset_id, tag_id)` serves asset_id-prefix lookups.
- **Custom-SQL filter interpolation** (`filter_model.py:182-185`): by design (documented feature); read connection is `PRAGMA query_only = ON`. Errors surface via `on_error` reset (`ui_asset_table.py:243-248`).
- **Unbounded DBWorker queue:** submissions are user-action-driven and the backfill keeps exactly one batch in flight; backpressure machinery would be unrequested complexity.
- **`is_running` GIL-atomicity pattern:** intentional and documented in CLAUDE.md.
- **Detail panel refreshing after mutations:** it does (`ui_detail_panel.py:583-586`) — contrary to first appearances; the gap is documentation (5.3), plus over-fetching (3.3).
- **Pagination materializing all rows:** already fixed since the last audit — SQL LIMIT/OFFSET at `federation.py:946-954`.
- **`ATTACH ... AS <label>` interpolation:** guarded by `_validate_label` regex; the one sanctioned identifier interpolation.

## Verification

1. After every phase: `cd src && python -m unittest discover -p "test_*.py"` — must stay green (161+ tests).
2. After Phase 3: launch GUI (`cd src && python imgdb_ui.py`), type a multi-word value into a filter field — table must update once after typing stops, not per keystroke. Enable phash backfill with batch size 50 — scrolling and detail-panel loads must stay responsive during backfill.
3. After Phase 1: Import from caption → scope "all filtered" → ambiguous handling "Ask per tag" — must show the resolution dialog instead of an error box.
4. After Phase 4: `grep -rn "execute(" src/ui_*.py src/imgdb_ui.py` returns nothing; `wc -l src/imgdb_ui.py` should drop substantially (~1524 → under ~800).
5. Manual smoke: add/remove tags rapidly on one asset (chips update, one suggestion rescan after burst), rename + delete an asset, run a scan, resize/restart to confirm ini persistence still works.
