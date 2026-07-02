# imgdb — federated image dataset catalog

A local GUI tool for cataloging image datasets across multiple root
directories.  Each root is its own independent SQLite database; a federation
layer presents them as a single queryable catalog.  Files and their metadata
stay inside their root directory — nothing is stored anywhere else.

## Design invariants

These rules are load-bearing.  Every component is built to enforce them.

1. **Physical compartmentalization.**  Each root directory owns its own
   catalog at `<root>/imgdb.sqlite` and thumbnail cache at
   `<root>/imgdb_thumbs/`.  Detaching a drive detaches the whole catalog for
   that drive.
2. **No hidden files or directories.**  Nothing is written to `~/.cache`,
   `/tmp`, or any dotfile.  Everything imgdb creates is visible in a normal
   `ls`.
3. **One file = one asset.**  Two files with identical content are two
   separate assets.  Deduplication is an explicit user operation (`merge`),
   never automatic.
4. **Stable logical identity.**  Each asset has a UUID `asset_id` that never
   changes.  Content hashes are mutable metadata, not identity.
5. **Atomic disk + DB operations.**  Disk operations are staged to a sibling
   `.imgdb-tmp-<uuid>` file, the DB transaction commits, then the staging file
   is finalised.  Failures roll back cleanly.
6. **No silent fallbacks.**  Missing dependencies error at import time.
   Re-attaching a label to a different path errors.  Cross-shard writes error.
7. **Strict layering.**  Lower layers have no knowledge of higher ones.  See
   Architecture below.
8. **Cross-shard writes are forbidden.**  Every write routes to exactly one
   shard.  Merging assets across roots is not allowed.
9. **Data structures carry their full context.**  Schemas and in-memory
   structures include enough fields to support future consumers without
   requiring a second query.  Narrow return types (e.g. `(name, count)`)
   are upgraded when a new caller needs more (e.g. `(name, type, count)`)
   rather than adding a parallel lookup.

## Architecture

```
imgdb.py          Per-shard library.  Single SQLite database.  No knowledge of
                  other shards, federation, or UI.

federation.py     Federation layer.  Opens all attached shards, builds an
                  in-memory asset_id→shard index, creates temporary union
                  views (all_assets, all_captions, all_tags, etc.) on a
                  read-only connection, and routes writes to exactly one shard.

imgdb_thumbs.py   Toolkit-agnostic thumbnail worker.  Single background thread,
                  two priority levels (VISIBLE / BACKGROUND), no Qt imports.

imgdb_worker.py   Toolkit-agnostic DB worker.  Wraps a Federation in a single
                  background thread with a FIFO job queue.

imgdb_worker_qt.py / imgdb_thumbs_qt.py
                  Qt adapters only.  Marshal callbacks from worker threads to
                  Qt signals on the main thread.  No business logic.

imgdb_ui.py       Main GUI window.  Imports from all layers above.
ui_*.py           Individual UI panels and dialogs.
```

Layers never import upward.  `imgdb.py` has no awareness of the federation,
and neither library layer has any awareness of Qt.

## Requirements

```
Python 3.10+
pip install blake3 Pillow PySide6
```

## Running

```bash
# Launch the GUI
cd src
python imgdb_ui.py
```

Configuration is stored in `imgdb.conf` (INI format) next to the entry point.
GUI window settings (geometry, column widths, sort order) are stored in
`imgdb_ui.ini` in the same directory.

## GUI overview

The main window is split into three panes:

- **Left** — root/shard list.  Per-root checkboxes control which shards are
  included in filter results and bulk operations.
- **Center** — virtualized, sortable asset table with filter bar.  Filtering
  supports a freeform SQL `WHERE` clause, an active dataset selector,
  AND-combined tag filters, and a show/hide missing-file toggle.
- **Right** — tabbed panel with Image Properties and Tag Management.

### Image properties tab

Shows thumbnail, file metadata, and dataset membership for the selected asset.

**Tags** are organised by category (type).  Any number of named categories
can coexist; "General" is always present.  Tags can be added by typing into
the input field under each category, removed via their chip × button, or
clicked to add a tag filter.  "Import from caption…" extracts matching tags
from the asset's caption text (see below).

**Captions** are free-form text per named kind (e.g. `booru`, `description`,
`short`).  One caption per kind per asset, enforced at the DB level.  Captions
are edited inline and save on focus-out.  Individual captions can be deleted
with the − button next to the kind label.

### Tag management tab

Shows all tags used by assets in the current filter set, with columns for
name, category, and usage count.  Bulk operations:

- **Replace tag** — rename a tag globally within its shard.
- **Delete tag** — remove a tag and all its asset links globally.
- **Change tag type** — reassign a tag to a different category (existing types
  or a new one typed inline).
- **Add to / Remove from selection** — modify tags on the currently selected
  assets.
- **Add to / Remove from filtered** — modify tags on every asset matching the
  current filter.
- **Tag filters** — AND-combined tag filter list that constrains the asset
  table; filters can be added from the selected tag, typed manually, or
  cleared.

### Import tags from caption

**Edit: Claude wrote this doc. I'm leaving this bit uncorrected because it's really fucking funny**
"Import from caption…" opens a dialog that matches known tags against an
asset's caption text using word-boundary-aware n-gram tokenization — substring
false-positives like "red" inside "already" are excluded automatically.

Options:

- **Caption** — which caption kind to extract from, or "All captions"
  (concatenates all kinds).
- **Scope** — this image only, all filtered images, or all images in checked
  shards.  Assets that do not have the selected caption kind are skipped.
- **Ambiguous tag handling** — when a tag name exists in more than one
  category, either import as General (default) or ask per tag.  In ask mode,
  a single resolution dialog covers every unique ambiguous name found across
  the scope; the chosen category is applied consistently to all assets.

For single-image scope a live preview checklist shows matched tags before
applying, with per-row checkboxes and per-row category selectors for ambiguous
tags.

## Query mode

Switching the toolbar to **Query** exposes the federation's read-only
connection directly.  It runs a full `SELECT` with every attached shard
ATTACH-ed and all `all_*` union views available, and can save a result set as a
named dataset.

#### Example queries

```sql
-- All assets across all shards
SELECT _root, asset_id, rel_path FROM all_assets

-- Assets tagged 'landscape' anywhere
SELECT at._root, at.asset_id
FROM all_asset_tags at
JOIN all_tags t ON t._root = at._root AND t.tag_id = at.tag_id
WHERE t.name = 'landscape'

-- Missing files from the last scan
SELECT _root, asset_id, rel_path FROM all_assets WHERE exists_flag = 0

-- Duplicate content within a shard
SELECT current_hash, COUNT(*) AS n FROM main.assets
GROUP BY current_hash HAVING n > 1
```

Caption full-text search uses FTS5.  Because FTS5 virtual tables cannot be
exposed through union views, caption search fans out per-shard in Python
(`federation.search_captions`) rather than through the union views above.

## Schema overview (per shard)

- **`assets`** — one row per tracked file.  Key columns: `asset_id` (UUID),
  `rel_path` (UNIQUE), `current_hash`, `width`, `height`, `format`, `bytes`,
  `last_seen`, `exists_flag`.
- **`asset_hash_history`** — prior content hashes from in-place edits.
- **`captions`** — `(asset_id, kind, content)`.  UNIQUE on `(asset_id, kind)`.
- **`captions_fts`** — FTS5 external-content index over `captions.content`.
- **`tag_types`** — named tag categories (`General`, `Artist`, etc.).
- **`tags`** — `(tag_id, name, type_id)`.  A tag belongs to exactly one
  category per shard.
- **`asset_tags`** — many-to-many join between assets and tags.
- **`datasets`** and **`dataset_assets`** — named collections of assets.
- **`merged_assets`** — merge history; old `asset_id`s continue to resolve
  via the in-memory index.

Federation union views (`all_assets`, `all_captions`, `all_tags`,
`all_asset_tags`, `all_tag_types`, `all_dataset_assets`, etc.) are temporary
views on the read-only connection; they exist only for the lifetime of the
process.

## Files created by imgdb

- `imgdb.conf` — per-machine config listing attached roots (next to entry point).
- `imgdb_ui.ini` — GUI settings: geometry, column widths, sort order (next to entry point).
- `<root>/imgdb.sqlite` (+ `-wal`, `-shm` sidecars) — one per root.
- `<root>/imgdb_thumbs/` — thumbnail cache.

Nothing else.  No hidden files.  No state in `~/.config` or `~/.cache`.
