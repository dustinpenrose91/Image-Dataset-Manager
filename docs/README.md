# imgdb — federated image dataset catalog

A local command-line tool for cataloging image datasets across multiple
root directories. Each root is its own independent SQLite database; a
federation layer presents them as a single queryable catalog. Files and
their metadata stay with their root and nowhere else.

## Design invariants (non-negotiable)

These are the rules the code is built on. Read them before making changes.

1. **Physical compartmentalization.** Each root directory owns its own
   SQLite catalog at `<root>/imgdb.sqlite` and its own derived-data
   directory at `<root>/imgdb_thumbs/`. Nothing about a root's contents
   is stored anywhere else on the filesystem — not in a central DB, not
   in a cache directory, not in the user's home. Detaching a drive
   detaches the whole catalog for that drive.
2. **No hidden files or directories.** Anywhere imgdb creates a file or
   directory, it is visible in a normal `ls`. No dotfiles, no `~/.cache`
   entries, no `/tmp` scratch files that outlive a command.
3. **One file = one asset.** Two files with identical content are two
   assets. Deduplication is a user operation via `merge`, never automatic.
4. **Stable logical identity.** Each asset has a UUID `asset_id` that
   never changes across content edits. Content hashes are a mutable
   attribute, not an identity.
5. **Atomic disk + DB operations.** Rename and delete open a write
   transaction, perform the disk operation, and commit only on success.
6. **No silent fallbacks.** Missing dependencies error at import time.
   Re-attaching a root label to a different path errors. Cross-shard
   merges error.
7. **Strict layering.** `imgdb.py` is per-shard library code only.
   `federation.py` is the cross-shard layer. `imgdb_cli.py` is a thin
   argv-to-function mapping. A future UI imports from the library
   modules only.
8. **Cross-shard writes are forbidden.** Every write routes to exactly
   one shard. Merging assets across roots is not allowed.

## Architecture

```
./imgdb.conf                      per-machine config (label -> abs_path)
./imgdb_cli.py                    thin CLI
./imgdb.py                        per-shard library
./federation.py                   federation layer

<root1>/imgdb.sqlite              root1's catalog
<root1>/imgdb_thumbs/             root1's derived data (future)
<root1>/...                       root1's image files

<root2>/imgdb.sqlite              root2's catalog
<root2>/imgdb_thumbs/             root2's derived data
<root2>/...                       root2's image files
```

There is no central database. `imgdb.conf` lists which roots are
currently attached on this machine. Each shard is a full, self-contained
imgdb catalog. If a shard file is leaked, it reveals only its own root's
metadata — nothing about other roots.

## Requirements

- Python 3.10 or newer
- `pip install blake3 Pillow`

## Quick start

```bash
# Attach a root (registers it in ./imgdb.conf and initializes the shard)
python imgdb_cli.py root attach main /home/user/photos

# Attach another
python imgdb_cli.py root attach archive /mnt/external/archive

# See what's attached
python imgdb_cli.py root list

# Scan a root
python imgdb_cli.py scan main

# Tag an asset
python imgdb_cli.py tag add <asset_id> landscape sunset

# Set captions
python imgdb_cli.py caption set <asset_id> short "Sunset at the ridge"
python imgdb_cli.py caption set <asset_id> long --from-file description.txt

# Query across all attached shards
python imgdb_cli.py query "SELECT _root, asset_id FROM all_assets LIMIT 50"

# Caption full-text search across all attached shards
python imgdb_cli.py fts "sunset AND mountain"
```

## How content edits are tracked

When you scan a root, each file is classified as one of:

- **new** — rel_path not seen before in this shard; create a new asset.
- **unchanged** — known rel_path, same content hash. Just touch `last_seen`.
- **edited** — known rel_path, different content hash. Move the old hash
  to `asset_hash_history` and update `current_hash`. Tags, captions, and
  the `asset_id` all survive the edit.

A fourth state, **missing**, is detected after the walk: files that were
present on the last scan but are gone now have `exists_flag = 0`.

## Cross-shard behavior

The federation layer opens every attached shard and exposes them through:

- **Per-shard SQL**: write `SELECT ... FROM <label>.assets` to query one
  shard directly.
- **Union views**: `all_assets`, `all_captions`, `all_tags`,
  `all_asset_tags`, `all_asset_hash_history`, `all_merged_assets` —
  each a `UNION ALL` across every attached shard, with an extra `_root`
  column identifying which shard each row came from. These views are
  created fresh at the start of each CLI invocation and exist only for
  the lifetime of that process.
- **Caption FTS (`fts` command)**: fans the search expression out to
  each shard's FTS5 index and unions the results in Python. FTS5 virtual
  tables cannot be unioned via views, so this is a dedicated command
  rather than a view.

Writes are routed by an in-memory `asset_id -> shard` index built at
startup. For tens of thousands of assets the index is a few megabytes;
for a million assets, around 115 MB. The index covers both live asset
IDs and historical (merged) IDs, so old references still resolve.

## Unavailable shards

If a shard's root directory isn't present (e.g. drive not mounted), the
federation logs a warning to stderr and continues with the shards that
are available. Queries against `all_*` views see only the attached
shards. Writes targeting an unavailable shard fail with a clear error.

## Commands

All commands accept `--config <path>` (default `./imgdb.conf`).

### Root management

```
imgdb_cli.py root attach <label> <abs_path>
imgdb_cli.py root detach <label>
imgdb_cli.py root list
```

Labels must match `[A-Za-z_][A-Za-z0-9_]*` because they're used as
SQLite schema names in ATTACH and in user SQL queries. Re-attaching a
label to a different path is rejected; detach first.

`detach` removes the label from `./imgdb.conf` only. The shard database
and the files under the root are not touched.

### scan

```
imgdb_cli.py scan <label> [--ext .jpg,.png,...] [-v]
```

Walks the root, ingests files, and updates the shard. Output:

```
<new> new, <edited> edited, <unchanged> unchanged, <missing> missing
```

### merge

```
imgdb_cli.py merge <survivor_id> <merged_id>
```

Merges metadata from `merged_id` into `survivor_id`. Both must belong to
the same shard. Rules:

- Tags: union.
- Captions: per kind, keep the longer content. Survivor wins ties.
- Hash history: merged asset's current and prior hashes move to survivor.
- Old `asset_id` is preserved in `merged_assets` and still resolves.
- **The file formerly associated with `merged_id` is NOT deleted.**
  Merge is a metadata operation. If you want the file gone, run
  `delete` on it first or delete it separately. This is intentional —
  the user's two files on disk haven't merged, only their metadata has.

### rename

```
imgdb_cli.py rename <asset_id> <new_rel_path>
```

Renames the file on disk and updates the DB in a single transaction.
`<new_rel_path>` is relative to the asset's root.

### delete

```
imgdb_cli.py delete <asset_id>
```

Deletes the file on disk and removes the asset row, in a single
transaction.

### tag

```
imgdb_cli.py tag add    <asset_id> <tag> [<tag> ...]
imgdb_cli.py tag remove <asset_id> <tag> [<tag> ...]
```

Tag names are case-insensitive, flat namespace, per shard. A tag named
"landscape" in `main` is a different row from a tag named "landscape"
in `archive`, but queries against `all_tags` will see both.

### caption

```
imgdb_cli.py caption set    <asset_id> <kind> <content>
imgdb_cli.py caption set    <asset_id> <kind> --from-file <path>
imgdb_cli.py caption delete <asset_id> <kind>
```

Free-form `kind` (e.g. `short`, `long`, `alt_de`). One caption per kind
per asset. `set` creates or updates.

### query

```
imgdb_cli.py query "<full SELECT statement>"
                   [--limit N | --no-limit]
                   [--output tsv|csv|json]
                   [--quiet]
```

Runs a full SELECT against the federation's read connection. The read
connection:

- Has every attached shard ATTACH-ed as `<label>.<table>`.
- Has `all_*` temporary union views covering every shard.
- Has `PRAGMA query_only = ON` — writes are impossible at the engine
  level, not just by convention.

**Default limit.** The CLI wraps the query with `LIMIT 20` unless:

- `--limit N` sets a different limit
- `--limit 0` or `--no-limit` removes the limit
- The query already contains its own `LIMIT`

A helper line is printed to stderr before each query showing the
effective limit and output format. Suppress with `--quiet`.

**Errors.** Malformed SQL prints:

```
SQL error: <sqlite message>
Query:     <the SQL that was run>
```

and exits with code 2.

#### Example queries

```sql
-- All assets across all attached shards
SELECT _root, asset_id, rel_path, current_hash FROM all_assets

-- Assets from one specific shard
SELECT asset_id, rel_path FROM main.assets

-- Assets tagged 'landscape' anywhere
SELECT at._root, at.asset_id
FROM all_asset_tags at
JOIN all_tags t
  ON t._root = at._root AND t.tag_id = at.tag_id
WHERE t.name = 'landscape'

-- Same query against a single shard
SELECT a.asset_id FROM main.assets a
JOIN main.asset_tags at ON at.asset_id = a.asset_id
JOIN main.tags t ON t.tag_id = at.tag_id
WHERE t.name = 'landscape'

-- Duplicate content within a shard (content hash match)
SELECT current_hash, COUNT(*) AS n FROM main.assets
GROUP BY current_hash HAVING n > 1

-- Missing files from the last scan
SELECT _root, asset_id, rel_path FROM all_assets WHERE exists_flag = 0
```

### fts

```
imgdb_cli.py fts "<FTS5 match expression>"
```

Runs an FTS5 MATCH across every attached shard's captions and prints
tab-separated results: `shard<TAB>asset_id<TAB>kind<TAB>content`.

Examples:

```bash
imgdb_cli.py fts "sunset"
imgdb_cli.py fts '"golden hour"'
imgdb_cli.py fts "mountain AND snow NOT crowd"
imgdb_cli.py fts "land*"
```

FTS results cannot be combined with tag or shard filters in the same
query. For complex combined searches, run `fts` to narrow down and then
`query` to join against the resulting asset_ids.

## Schema overview (per shard)

Each shard's database contains:

- `assets (asset_id, rel_path, current_hash, phash, width, height,
  format, bytes, last_seen, exists_flag, ...)` — primary table.
  `rel_path` is UNIQUE within the shard. `current_hash` is NOT unique.
- `asset_hash_history (asset_id, hash, replaced_at)` — prior content
  hashes from in-place edits.
- `captions (caption_id, asset_id, kind, content, updated_at)` — one
  row per asset per kind.
- `captions_fts` — FTS5 external-content index over `captions.content`.
- `tags (tag_id, name)` and `asset_tags (asset_id, tag_id)`.
- `merged_assets (old_asset_id, new_asset_id, merged_at)` — merge
  history for this shard's assets.

`phash` is reserved for future perceptual-hash work and is currently
unused.

## Files created by imgdb

- `./imgdb.conf` — per-machine config listing attached roots.
- `<root>/imgdb.sqlite` (+`-wal`, `-shm` sidecars) — one per root.
- `<root>/imgdb_thumbs/` — reserved for future derived data.

Nothing else. No hidden files. No state in `~/.config` or `~/.cache`.

## `.gitignore` note

The provided `.gitignore` excludes the config file and the shard
database files from any project working directory. If a root directory
is itself inside a git repo, add `imgdb.sqlite*` and `imgdb_thumbs/` to
that repo's `.gitignore` as well.
