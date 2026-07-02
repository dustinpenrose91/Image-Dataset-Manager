"""
federation — unified query and write interface over multiple imgdb shards.

Each shard is a self-contained SQLite file living inside its own root
directory. The federation layer:

    - Loads a per-machine config file listing attached roots (label -> path)
    - Opens every attached shard and keeps each shard's own dedicated
      connection for writes
    - Maintains an additional ATTACH-ed read connection that binds every
      shard as `<label>.<table>` for cross-shard SQL queries
    - Creates temporary union views (`all_assets`, `all_captions`, ...)
      over the attached shards on that read connection
    - Maintains an in-memory asset_id -> shard_label index so writes to an
      asset_id can be routed to the correct shard without the user having
      to specify it
    - Propagates missing shards (drive not mounted, directory gone) as
      warnings rather than hard errors, so other roots stay usable

Design invariants:
    - Writes are routed to exactly one shard. Cross-shard merges are
      rejected.
    - The federation layer owns no schema of its own. It creates only
      temporary objects (views) on a connection it owns.
    - Config is plain text at ./imgdb.conf (INI format). No hidden files.
"""

from __future__ import annotations

import configparser
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, Optional

import imgdb
from filter_model import FilterRule, SortRule, build_filter_conditions, build_sort_clause

DEFAULT_CONFIG_PATH = "./imgdb.conf"
CONFIG_SECTION = "roots"

# The set of per-shard tables that get unioned into `all_*` views.
UNION_TABLES: tuple[str, ...] = (
    "assets",
    "asset_hash_history",
    "captions",
    "tag_types",
    "tags",
    "asset_tags",
    "merged_assets",
    "datasets",
    "dataset_assets",
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class FederationError(imgdb.ImgDBError):
    pass


class ConfigError(FederationError):
    pass


class RootNotFoundError(FederationError):
    pass


class RootAlreadyExistsError(FederationError):
    pass


class ShardUnavailableError(FederationError):
    pass


class CrossShardOperationError(FederationError):
    pass


class OverlappingRootError(FederationError):
    """Raised when attempting to attach a root that overlaps an existing one."""


class AmbiguousAssetError(FederationError):
    """Raised if an asset_id somehow exists in more than one shard."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RootEntry:
    label: str
    abs_path: str


# SQLite reserves these schema names; they cannot be used as ATTACH aliases.
_RESERVED_SCHEMA_NAMES = frozenset({"main", "temp"})


def _validate_label(label: str) -> None:
    """
    Labels are used as SQLite schema names via ATTACH DATABASE. SQLite
    accepts identifiers but we also use them in user SQL, so restrict to
    [A-Za-z_][A-Za-z0-9_]*. This prevents injection via label names.
    """
    if not label:
        raise ConfigError("Root label may not be empty")
    if label.lower() in _RESERVED_SCHEMA_NAMES:
        raise ConfigError(
            f"Root label {label!r} is a SQLite reserved schema name "
            f"(reserved: {sorted(_RESERVED_SCHEMA_NAMES)}). Choose another."
        )
    if not (label[0].isalpha() or label[0] == "_"):
        raise ConfigError(
            f"Root label {label!r} must start with a letter or underscore"
        )
    for ch in label:
        if not (ch.isalnum() or ch == "_"):
            raise ConfigError(
                f"Root label {label!r} contains invalid character {ch!r}; "
                f"allowed: letters, digits, underscore"
            )


def load_config(path: str = DEFAULT_CONFIG_PATH) -> list[RootEntry]:
    """Read the config file and return the list of declared roots."""
    if not os.path.exists(path):
        return []
    parser = configparser.ConfigParser()
    try:
        parser.read(path)
    except configparser.Error as e:
        raise ConfigError(f"Failed to parse config {path!r}: {e}") from e
    if CONFIG_SECTION not in parser:
        return []
    entries: list[RootEntry] = []
    seen: set[str] = set()
    for label, abs_path in parser[CONFIG_SECTION].items():
        _validate_label(label)
        if label in seen:
            raise ConfigError(f"Duplicate root label in config: {label!r}")
        seen.add(label)
        entries.append(RootEntry(label=label, abs_path=os.path.abspath(abs_path)))
    return entries


def save_config(entries: Iterable[RootEntry], path: str = DEFAULT_CONFIG_PATH) -> None:
    """Write the given root entries to the config file."""
    parser = configparser.ConfigParser()
    parser[CONFIG_SECTION] = {e.label: e.abs_path for e in entries}
    with open(path, "w") as f:
        parser.write(f)


def _paths_overlap(a: str, b: str) -> bool:
    """
    True if `a` and `b` are the same directory or one is contained within
    the other. Uses normalized real paths to defeat symlink games and
    trailing-slash differences.

    Two roots overlapping is a hard error: the inner root's images would
    be scanned twice (once by each shard), creating duplicate ingestion
    of the same file with different asset_ids in different shards.
    """
    a = os.path.realpath(a)
    b = os.path.realpath(b)
    if a == b:
        return True
    # On case-insensitive filesystems this would need normcase, but the
    # CLI is documented as Linux-first so case-sensitive comparison is
    # correct here.
    a_sep = a.rstrip(os.sep) + os.sep
    b_sep = b.rstrip(os.sep) + os.sep
    return a_sep.startswith(b_sep) or b_sep.startswith(a_sep)


def add_root_to_config(
    label: str, abs_path: str, path: str = DEFAULT_CONFIG_PATH
) -> RootEntry:
    _validate_label(label)
    abs_path = os.path.abspath(abs_path)
    entries = load_config(path)
    for e in entries:
        if e.label == label:
            if os.path.abspath(e.abs_path) == abs_path:
                return e  # idempotent: same label+path is a no-op
            raise RootAlreadyExistsError(
                f"Root label {label!r} already present in config at "
                f"{e.abs_path!r}; refusing to rebind to {abs_path!r}. "
                f"Detach it first if relocation is intended."
            )
        if _paths_overlap(e.abs_path, abs_path):
            raise OverlappingRootError(
                f"Refusing to attach {abs_path!r}: it overlaps existing "
                f"root {e.label!r} at {e.abs_path!r}. Roots may not be "
                f"identical or nested within one another, because the "
                f"overlapping files would be scanned by both shards and "
                f"ingested as separate assets."
            )
    entry = RootEntry(label=label, abs_path=abs_path)
    entries.append(entry)
    save_config(entries, path)
    return entry


def relocate_root(fed: Federation, label: str, new_abs_path: str) -> None:
    """
    Update the path for an existing root label in the config.
    The new path must contain the shard database so we know it's the right dir.
    """
    new_abs_path = os.path.abspath(new_abs_path)
    db_path = os.path.join(new_abs_path, imgdb.SHARD_DB_FILENAME)
    if not os.path.isfile(db_path):
        raise FederationError(
            f"No shard database found at {db_path!r}. "
            f"Make sure you selected the correct directory."
        )
    entries = load_config(fed.config_path)
    new_entries = [
        RootEntry(label=e.label, abs_path=new_abs_path) if e.label == label else e
        for e in entries
    ]
    if new_entries == entries:
        raise RootNotFoundError(f"No root with label {label!r} in config")
    save_config(new_entries, fed.config_path)


def remove_root_from_config(
    label: str, path: str = DEFAULT_CONFIG_PATH
) -> None:
    entries = load_config(path)
    new_entries = [e for e in entries if e.label != label]
    if len(new_entries) == len(entries):
        raise RootNotFoundError(f"No root with label {label!r} in config")
    save_config(new_entries, path)


# ---------------------------------------------------------------------------
# Shard and federation
# ---------------------------------------------------------------------------

@dataclass
class Shard:
    """A single attached shard: its config entry + dedicated write connection."""
    label: str
    abs_path: str
    conn: sqlite3.Connection


@dataclass
class Federation:
    """
    An open federation across zero or more shards.

    - `shards`: label -> Shard. Only contains shards that were successfully
      opened. Missing or corrupt shards are recorded in `missing` with the
      reason.
    - `asset_index`: asset_id -> shard label. Includes both current and
      historical (merged) asset ids, so lookups by old ids still route.
    - `read_conn`: a separate SQLite connection with every shard ATTACH-ed
      as its label, plus `all_*` temporary views. Used exclusively for the
      user query command.
    """
    config_path: str
    shards: dict[str, Shard]
    missing: dict[str, str]           # label -> reason
    asset_index: dict[str, str]       # asset_id -> shard label
    read_conn: Optional[sqlite3.Connection]

    def close(self) -> None:
        for s in self.shards.values():
            try:
                s.conn.close()
            except Exception:
                pass
        self.shards.clear()
        if self.read_conn is not None:
            try:
                self.read_conn.close()
            except Exception:
                pass
            self.read_conn = None

    def __enter__(self) -> "Federation":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def open_federation(
    config_path: str = DEFAULT_CONFIG_PATH,
    on_warning: Optional[Callable[[str], None]] = None,
) -> Federation:
    """
    Load the config, open every shard, build the asset index, and prepare
    the read connection with union views.

    `on_warning(msg)` is called for each shard that could not be opened;
    the federation continues with whatever shards did open.
    """
    entries = load_config(config_path)
    shards: dict[str, Shard] = {}
    missing: dict[str, str] = {}

    for entry in entries:
        db_path = imgdb.shard_db_path(entry.abs_path)
        if not os.path.isdir(entry.abs_path):
            reason = f"root directory not found: {entry.abs_path!r}"
            missing[entry.label] = reason
            if on_warning:
                on_warning(f"shard {entry.label!r} unavailable: {reason}")
            continue
        # init_shard creates the DB if absent and runs migrations if needed.
        # It is idempotent on existing databases (all DDL uses IF NOT EXISTS).
        try:
            conn = imgdb.init_shard(entry.abs_path)
        except Exception as e:
            reason = f"failed to open shard DB: {e}"
            missing[entry.label] = reason
            if on_warning:
                on_warning(f"shard {entry.label!r} unavailable: {reason}")
            continue
        shards[entry.label] = Shard(label=entry.label, abs_path=entry.abs_path, conn=conn)

    asset_index = _build_asset_index(shards)
    if len(shards) > SQLITE_DEFAULT_MAX_ATTACHED and on_warning:
        on_warning(
            f"federation has {len(shards)} shards; SQLite's default "
            f"SQLITE_MAX_ATTACHED is {SQLITE_DEFAULT_MAX_ATTACHED}. "
            f"Cross-shard read queries may fail unless SQLite is built "
            f"with a higher limit (max 125)."
        )
    read_conn = _open_read_connection(shards)

    return Federation(
        config_path=config_path,
        shards=shards,
        missing=missing,
        asset_index=asset_index,
        read_conn=read_conn,
    )


def _build_asset_index(shards: dict[str, Shard]) -> dict[str, str]:
    """
    Build asset_id -> shard_label for every live and historical asset id
    across attached shards.
    """
    index: dict[str, str] = {}
    for label, shard in shards.items():
        for aid in imgdb.list_asset_ids(shard.conn):
            if aid in index and index[aid] != label:
                raise AmbiguousAssetError(
                    f"Asset id {aid!r} found in both {index[aid]!r} and {label!r}"
                )
            index[aid] = label
        for old_id, _new_id in imgdb.list_merged_ids(shard.conn):
            # Historical ids route to the same shard as the current asset.
            # No ambiguity check here — historical ids belong to the shard
            # where the merge happened.
            index.setdefault(old_id, label)
    return index


# SQLite's default compile-time SQLITE_MAX_ATTACHED is 10. Going above this
# requires a custom build. We surface a warning at the threshold.
SQLITE_DEFAULT_MAX_ATTACHED = 10


def _open_read_connection(
    shards: dict[str, Shard]
) -> Optional[sqlite3.Connection]:
    """
    Open a read connection and ATTACH every shard as its label. Create the
    union temporary views. Returns None if there are zero shards.

    Note on query_only: we enable it AFTER creating the TEMP views, because
    creating views (even temporary ones) counts as a schema write. Once
    set, no further writes of any kind can occur on this connection.
    """
    if not shards:
        return None
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for label, shard in shards.items():
        db_path = imgdb.shard_db_path(shard.abs_path)
        # Label has been validated to be a safe identifier, so interpolation
        # is safe here. SQLite ATTACH doesn't support parameter binding for
        # the schema name.
        conn.execute(f"ATTACH DATABASE ? AS {label}", (db_path,))
    _create_union_views(conn, list(shards.keys()))
    conn.execute("PRAGMA query_only = ON")
    return conn


def _create_union_views(
    conn: sqlite3.Connection, labels: list[str]
) -> None:
    """
    Create temporary union views spanning all attached shards.
    Each view is `all_<table>` and has an extra `_root` column identifying
    which shard each row came from.
    """
    if not labels:
        return
    for table in UNION_TABLES:
        parts = [
            f"SELECT '{label}' AS _root, * FROM {label}.{table}"
            for label in labels
        ]
        sql = f"CREATE TEMP VIEW all_{table} AS\n" + "\nUNION ALL\n".join(parts)
        conn.execute(sql)


# ---------------------------------------------------------------------------
# Routing and lookup
# ---------------------------------------------------------------------------

def shard_for_asset(fed: Federation, asset_id: str) -> Shard:
    """Return the shard owning the given asset_id, routing via the index."""
    label = fed.asset_index.get(asset_id)
    if label is None:
        raise imgdb.AssetNotFoundError(
            f"No asset with id {asset_id!r} in any attached shard"
        )
    shard = fed.shards.get(label)
    if shard is None:
        raise ShardUnavailableError(
            f"Asset {asset_id!r} belongs to shard {label!r}, which is "
            f"currently unavailable"
        )
    return shard


def find_asset_by_abs_path(
    fed: Federation, abs_path: str
) -> Optional[tuple[Shard, "imgdb.Asset"]]:
    """
    Locate the (shard, asset) whose file lives at abs_path, or None if abs_path
    is not inside any attached root or no asset is registered there. Subsumes the
    "iterate shards, relativize, look up by rel_path" pattern used by preview/
    mask/perceptual-hash handlers.
    """
    for shard in fed.shards.values():
        try:
            rel = os.path.relpath(abs_path, shard.abs_path).replace(os.sep, "/")
        except ValueError:  # different drive on Windows
            continue
        if rel.startswith("../"):
            continue
        asset = imgdb.get_asset_by_rel_path(shard.conn, rel)
        if asset is not None:
            return shard, asset
    return None


def set_has_mask_by_abs_path(fed: Federation, abs_path: str, has_mask: bool) -> bool:
    """Set has_mask for the asset at abs_path. Returns True if an asset matched."""
    found = find_asset_by_abs_path(fed, abs_path)
    if found is None:
        return False
    shard, asset = found
    imgdb.set_has_mask(shard.conn, asset.asset_id, has_mask)
    return True


def set_perceptual_hash_by_abs_path(fed: Federation, abs_path: str, phash: str) -> bool:
    """Set perceptual_hash for the asset at abs_path. Returns True if matched."""
    found = find_asset_by_abs_path(fed, abs_path)
    if found is None:
        return False
    shard, asset = found
    imgdb.set_perceptual_hash(shard.conn, asset.asset_id, phash)
    return True


def shard_by_label(fed: Federation, label: str) -> Shard:
    shard = fed.shards.get(label)
    if shard is None:
        if label in fed.missing:
            raise ShardUnavailableError(
                f"Shard {label!r} is unavailable: {fed.missing[label]}"
            )
        raise RootNotFoundError(f"No shard with label {label!r}")
    return shard


def list_roots(fed: Federation) -> list[tuple[str, str, str]]:
    """
    Return a list of (label, abs_path, status) for every root in the config,
    in config order. Status is 'ok' or the reason the shard is unavailable.
    """
    entries = load_config(fed.config_path)
    out: list[tuple[str, str, str]] = []
    for e in entries:
        if e.label in fed.shards:
            out.append((e.label, e.abs_path, "ok"))
        else:
            out.append((e.label, e.abs_path, fed.missing.get(e.label, "unknown")))
    return out


# ---------------------------------------------------------------------------
# Post-open maintenance of the asset index
# ---------------------------------------------------------------------------

def _register_asset(fed: Federation, label: str, asset_id: str) -> None:
    fed.asset_index[asset_id] = label


def _forget_asset(fed: Federation, asset_id: str) -> None:
    fed.asset_index.pop(asset_id, None)


# ---------------------------------------------------------------------------
# Write operations — every one routes to exactly one shard
# ---------------------------------------------------------------------------

def attach_root(
    fed: Federation, label: str, abs_path: str
) -> RootEntry:
    """
    Register a new root in the config and initialize its shard. Does not
    reopen the federation; for the new root to appear in the live federation
    object the caller should reopen it. For the CLI this is fine because
    each command opens a fresh federation.
    """
    if label in fed.shards or label in fed.missing:
        raise RootAlreadyExistsError(
            f"Root label {label!r} already attached"
        )
    entry = add_root_to_config(label, abs_path, fed.config_path)
    # Initialize the shard eagerly so the first scan doesn't have to.
    init_conn = imgdb.init_shard(entry.abs_path)
    init_conn.close()
    return entry


def detach_root(fed: Federation, label: str) -> None:
    """
    Remove a root from the config. Does not touch the shard file on disk
    or any files under the root.
    """
    remove_root_from_config(label, fed.config_path)


def delete_root(fed: Federation, label: str) -> None:
    """
    Remove a root from the config AND delete its shard database and
    thumbnails directory. The image files themselves are not touched.
    Irreversible.
    """
    shard = shard_by_label(fed, label)
    abs_path = shard.abs_path
    try:
        shard.conn.close()
    except Exception:
        pass

    remove_root_from_config(label, fed.config_path)

    db_path = os.path.join(abs_path, imgdb.SHARD_DB_FILENAME)
    for suffix in ("", "-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            os.remove(p)

    thumbs_dir = os.path.join(abs_path, imgdb.THUMBS_DIRNAME)
    if os.path.isdir(thumbs_dir):
        shutil.rmtree(thumbs_dir)


def scan_shard(
    fed: Federation,
    label: str,
    extensions: frozenset[str] = imgdb.DEFAULT_EXTENSIONS,
    on_event: Optional[Callable[[str, str], None]] = None,
    cancel: Optional[object] = None,
) -> imgdb.ScanSummary:
    shard = shard_by_label(fed, label)
    summary, new_ids = imgdb.scan_root(
        shard.conn, shard.abs_path, extensions=extensions, on_event=on_event, cancel=cancel
    )
    # Only the newly created assets need index entries; existing rows are
    # already mapped, and updating them is wasted work on a million-row shard.
    for aid in new_ids:
        fed.asset_index[aid] = label
    return summary


# -- Incremental scan (interleaves with normal-priority user actions) ---------

def scan_shard_init(
    fed: Federation,
    label: str,
    extensions: frozenset[str] = imgdb.DEFAULT_EXTENSIONS,
) -> imgdb.ScanSession:
    """Phase 1: enumerate candidate files and prepare the seen-paths table."""
    shard = shard_by_label(fed, label)
    return imgdb.scan_root_init(shard.conn, shard.abs_path, extensions)


def scan_shard_batch(
    fed: Federation,
    label: str,
    session: imgdb.ScanSession,
    on_event: Optional[Callable[[str, str], None]] = None,
    cancel: Optional[object] = None,
) -> tuple[imgdb.ScanSession, bool]:
    """Phase 2: process one batch.  Returns (session, done)."""
    shard = shard_by_label(fed, label)
    done = imgdb.scan_root_batch(shard.conn, session, on_event=on_event, cancel=cancel)
    return session, done


def scan_shard_finish(
    fed: Federation,
    label: str,
    session: imgdb.ScanSession,
    on_event: Optional[Callable[[str, str], None]] = None,
) -> imgdb.ScanSummary:
    """Phase 3: mark missing files, update asset index, return summary."""
    shard = shard_by_label(fed, label)
    summary, new_ids = imgdb.scan_root_finish(shard.conn, session, on_event=on_event)
    for aid in new_ids:
        fed.asset_index[aid] = label
    return summary


def list_all_tag_types_federation(fed: Federation) -> list[str]:
    """Return all known tag type names across all shards, General first then by earliest creation."""
    if fed.read_conn is None:
        return ["General"]
    rows = fed.read_conn.execute("""
        SELECT name
          FROM all_tag_types
         GROUP BY name
         ORDER BY CASE WHEN MIN(type_id) = 0 THEN 0 ELSE 1 END,
                  MIN(created_at) ASC
    """).fetchall()
    return [r["name"] for r in rows] or ["General"]


def add_tags(
    fed: Federation,
    asset_id: str,
    tag_names: Iterable[str],
    type_name: str = "General",
) -> None:
    shard = shard_for_asset(fed, asset_id)
    imgdb.add_tags(shard.conn, asset_id, tag_names, type_name=type_name)


def remove_tags(fed: Federation, asset_id: str, tag_ids: Iterable[str]) -> None:
    """Remove tags by tag_id (UUID). Used for single-chip removal from the detail panel."""
    shard = shard_for_asset(fed, asset_id)
    imgdb.remove_tags(shard.conn, asset_id, tag_ids)


def remove_tags_by_name(
    fed: Federation,
    asset_id: str,
    tag_name: str,
    type_name: Optional[str] = None,
) -> None:
    """Remove tags by name. Used for batch operations where type is unknown."""
    shard = shard_for_asset(fed, asset_id)
    imgdb.remove_tags_by_name(shard.conn, asset_id, tag_name, type_name)


# ---------------------------------------------------------------------------
# Tag management (global operations on tag entities)
# ---------------------------------------------------------------------------

def list_tags_for_filtered_assets(
    fed: Federation,
    checked_labels: Optional[list[str]],
    filter_rules: list[FilterRule],
) -> list[tuple[str, str, int]]:
    """
    Return (tag_name, type_name, count) for every tag used by assets that
    match the current filter. Suitable for populating the tag management panel.
    """
    if fed.read_conn is None:
        return []

    conditions, params = build_filter_conditions(
        filter_rules, checked_labels, alias="all_assets"
    )
    if conditions == ["1=0"]:
        return []

    sql = (
        "SELECT t.name AS tag_name, tt.name AS type_name,"
        "       COUNT(DISTINCT all_assets.asset_id) AS cnt\n"
        "  FROM all_assets\n"
        "  JOIN all_asset_tags ata ON ata.asset_id = all_assets.asset_id\n"
        "  JOIN all_tags t ON t.tag_id = ata.tag_id\n"
        "  JOIN all_tag_types tt ON tt.type_id = t.type_id AND tt._root = t._root\n"
    )
    if conditions:
        sql += " WHERE " + " AND ".join(conditions) + "\n"
    sql += " GROUP BY t.name, tt.name ORDER BY cnt DESC, t.name ASC"

    rows = fed.read_conn.execute(sql, params).fetchall()
    return [(r["tag_name"], r["type_name"], r["cnt"]) for r in rows]


def replace_tag_globally(
    fed: Federation,
    old_name: str,
    old_type_name: str,
    new_name: str,
    new_type_name: str,
) -> None:
    """
    Rename/retype a tag across all shards. If the destination (new_name,
    new_type_name) already exists on some assets, the old tag is merged into
    it (duplicate asset links are discarded).
    """
    for shard in fed.shards.values():
        conn = shard.conn
        old_row = conn.execute(
            """SELECT t.tag_id FROM tags t
               JOIN tag_types tt ON t.type_id = tt.type_id
               WHERE t.name = ? AND tt.name = ?""",
            (old_name, old_type_name),
        ).fetchone()
        if old_row is None:
            continue
        old_tag_id = old_row["tag_id"]
        new_tag_id = imgdb.get_or_create_tag(conn, new_name, new_type_name)
        if new_tag_id == old_tag_id:
            continue
        with imgdb.transaction(conn):
            # Move links; OR IGNORE silently drops rows where the asset already
            # has the new tag (PK conflict on asset_tags).
            conn.execute(
                "UPDATE OR IGNORE asset_tags SET tag_id = ? WHERE tag_id = ?",
                (new_tag_id, old_tag_id),
            )
            conn.execute("DELETE FROM asset_tags WHERE tag_id = ?", (old_tag_id,))
            conn.execute("DELETE FROM tags WHERE tag_id = ?", (old_tag_id,))


def delete_tag_globally(fed: Federation, tag_name: str, type_name: str) -> None:
    """Remove a tag and all its asset links from every shard."""
    for shard in fed.shards.values():
        conn = shard.conn
        with imgdb.transaction(conn):
            conn.execute(
                """DELETE FROM asset_tags WHERE tag_id IN (
                       SELECT t.tag_id FROM tags t
                       JOIN tag_types tt ON t.type_id = tt.type_id
                       WHERE t.name = ? AND tt.name = ?
                   )""",
                (tag_name, type_name),
            )
            conn.execute(
                """DELETE FROM tags WHERE tag_id IN (
                       SELECT t.tag_id FROM tags t
                       JOIN tag_types tt ON t.type_id = tt.type_id
                       WHERE t.name = ? AND tt.name = ?
                   )""",
                (tag_name, type_name),
            )


def add_tag_to_asset_ids(
    fed: Federation,
    asset_ids: list[str],
    tag_name: str,
    type_name: str = "General",
) -> None:
    """Add a tag to specific assets by ID, grouped per shard for efficiency."""
    by_shard: dict[str, list[str]] = {}
    for aid in asset_ids:
        label = fed.asset_index.get(aid)
        if label:
            by_shard.setdefault(label, []).append(aid)
    for label, ids in by_shard.items():
        conn = fed.shards[label].conn
        tag_id = imgdb.get_or_create_tag(conn, tag_name, type_name)
        with imgdb.transaction(conn):
            for aid in ids:
                conn.execute(
                    "INSERT OR IGNORE INTO asset_tags(asset_id, tag_id) VALUES (?, ?)",
                    (aid, tag_id),
                )


def remove_tag_from_asset_ids(
    fed: Federation,
    asset_ids: list[str],
    tag_name: str,
    type_name: str,
) -> None:
    """Remove a specific (name, type) tag from a set of assets."""
    by_shard: dict[str, list[str]] = {}
    for aid in asset_ids:
        label = fed.asset_index.get(aid)
        if label:
            by_shard.setdefault(label, []).append(aid)
    for label, ids in by_shard.items():
        conn = fed.shards[label].conn
        ph = ",".join("?" * len(ids))
        with imgdb.transaction(conn):
            conn.execute(
                f"""DELETE FROM asset_tags
                    WHERE asset_id IN ({ph})
                    AND tag_id IN (
                        SELECT t.tag_id FROM tags t
                        JOIN tag_types tt ON t.type_id = tt.type_id
                        WHERE t.name = ? AND tt.name = ?
                    )""",
                [*ids, tag_name, type_name],
            )


def add_tag_to_filtered_assets(
    fed: Federation,
    tag_name: str,
    type_name: str,
    checked_labels: Optional[list[str]],
    filter_rules: list[FilterRule],
) -> None:
    """Add a tag to every asset matching the current filter."""
    by_shard: dict[str, list[str]] = {}
    for asset in list_filtered_assets(fed, checked_labels, filter_rules, []):
        by_shard.setdefault(asset.root, []).append(asset.asset_id)
    for label, ids in by_shard.items():
        conn = fed.shards[label].conn
        tag_id = imgdb.get_or_create_tag(conn, tag_name, type_name)
        with imgdb.transaction(conn):
            for aid in ids:
                conn.execute(
                    "INSERT OR IGNORE INTO asset_tags(asset_id, tag_id) VALUES (?, ?)",
                    (aid, tag_id),
                )


def remove_tag_from_filtered_assets(
    fed: Federation,
    tag_name: str,
    type_name: str,
    checked_labels: Optional[list[str]],
    filter_rules: list[FilterRule],
) -> None:
    """Remove a specific (name, type) tag from every asset matching the current filter."""
    by_shard: dict[str, list[str]] = {}
    for asset in list_filtered_assets(fed, checked_labels, filter_rules, []):
        by_shard.setdefault(asset.root, []).append(asset.asset_id)
    for label, ids in by_shard.items():
        if not ids:
            continue
        conn = fed.shards[label].conn
        ph = ",".join("?" * len(ids))
        with imgdb.transaction(conn):
            conn.execute(
                f"""DELETE FROM asset_tags
                    WHERE asset_id IN ({ph})
                    AND tag_id IN (
                        SELECT t.tag_id FROM tags t
                        JOIN tag_types tt ON t.type_id = tt.type_id
                        WHERE t.name = ? AND tt.name = ?
                    )""",
                [*ids, tag_name, type_name],
            )


def set_caption(
    fed: Federation, asset_id: str, kind: str, content: str
) -> None:
    shard = shard_for_asset(fed, asset_id)
    imgdb.set_caption(shard.conn, asset_id, kind, content)


def delete_caption(fed: Federation, asset_id: str, kind: str) -> None:
    shard = shard_for_asset(fed, asset_id)
    imgdb.delete_caption(shard.conn, asset_id, kind)


def rename_asset(
    fed: Federation, asset_id: str, new_rel_path: str, force: bool = False
) -> None:
    shard = shard_for_asset(fed, asset_id)
    imgdb.rename_asset(shard.conn, shard.abs_path, asset_id, new_rel_path, force=force)


def delete_asset(fed: Federation, asset_id: str) -> None:
    shard = shard_for_asset(fed, asset_id)
    imgdb.delete_asset(shard.conn, shard.abs_path, asset_id)
    _forget_asset(fed, asset_id)


def merge_assets(
    fed: Federation, survivor_id: str, merged_id: str
) -> None:
    """
    Merge two assets. Both must belong to the same shard — cross-shard
    merges are forbidden by design.
    """
    survivor_shard = shard_for_asset(fed, survivor_id)
    merged_shard = shard_for_asset(fed, merged_id)
    if survivor_shard.label != merged_shard.label:
        raise CrossShardOperationError(
            f"Cannot merge across shards: survivor {survivor_id!r} belongs "
            f"to {survivor_shard.label!r}, merged {merged_id!r} belongs to "
            f"{merged_shard.label!r}. Merges are scoped to a single root "
            f"by design."
        )
    imgdb.merge_assets(survivor_shard.conn, survivor_id, merged_id)
    # The merged asset_id still needs to resolve, so leave its index entry
    # pointing at the same shard. Just ensure the survivor is also mapped.
    _register_asset(fed, survivor_shard.label, survivor_id)


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

# Columns that the editing-tab list view is allowed to sort by. Restricted
# to a whitelist because the sort key is interpolated into SQL — we cannot

@dataclass(frozen=True)
class AssetRow:
    """A single row returned by list_filtered_assets."""
    asset_id: str
    root: str
    rel_path: str
    bytes: Optional[int]
    width: Optional[int]
    height: Optional[int]
    format: Optional[str]
    exists_flag: int
    perceptual_hash: Optional[str]


@dataclass(frozen=True)
class DatasetInfo:
    name: str
    description: str
    total_count: int
    shard_counts: dict  # label -> member_count


def list_filtered_assets(
    fed: Federation,
    checked_labels: Optional[list[str]],
    filter_rules: list[FilterRule],
    sort_rules: list[SortRule],
    limit: Optional[int] = None,
    offset: int = 0,
) -> Iterator[AssetRow]:
    """
    Stream asset rows from the federation filtered by checked roots and
    FilterRules. Pagination via limit/offset.

    A SQL syntax error in a custom-SQL FilterRule raises sqlite3.OperationalError;
    callers should surface the error and keep the previous filter.
    """
    if fed.read_conn is None:
        return

    conditions, params = build_filter_conditions(filter_rules, checked_labels, alias="a")
    if conditions == ["1=0"]:
        return

    order = build_sort_clause(sort_rules)
    sql_parts = [
        "SELECT a.asset_id, a._root, a.rel_path, a.bytes, a.width, a.height,"
        "       a.format, a.exists_flag, a.perceptual_hash",
        "FROM all_assets a",
    ]
    if conditions:
        sql_parts.append("WHERE " + " AND ".join(conditions))
    sql_parts.append(f"ORDER BY {order}")

    if limit is not None:
        sql_parts.append("LIMIT ?")
        params.append(limit)
        if offset:
            sql_parts.append("OFFSET ?")
            params.append(offset)
    elif offset:
        sql_parts.append("LIMIT -1 OFFSET ?")
        params.append(offset)

    cursor = fed.read_conn.execute("\n".join(sql_parts), params)
    while True:
        rows = cursor.fetchmany(1000)
        if not rows:
            break
        for r in rows:
            yield AssetRow(
                asset_id=r["asset_id"],
                root=r["_root"],
                rel_path=r["rel_path"],
                bytes=r["bytes"],
                width=r["width"],
                height=r["height"],
                format=r["format"],
                exists_flag=r["exists_flag"],
                perceptual_hash=r["perceptual_hash"],
            )


def count_filtered_assets(
    fed: Federation,
    checked_labels: Optional[list[str]],
    filter_rules: list[FilterRule],
) -> int:
    """Cheap row-count for the same filter list_filtered_assets uses."""
    if fed.read_conn is None:
        return 0

    conditions, params = build_filter_conditions(filter_rules, checked_labels, alias="a")
    if conditions == ["1=0"]:
        return 0

    sql = "SELECT COUNT(*) FROM all_assets a"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    return fed.read_conn.execute(sql, params).fetchone()[0]


def repair_missing_has_mask(fed: Federation, limit: int = 500) -> int:
    """
    Find existing assets where has_mask=0 but a mask file is present on disk,
    and update the DB to has_mask=1.  Returns the number of records repaired.

    This is a one-shot repair for assets that were masked before the has_mask
    column was introduced.
    """
    if fed.read_conn is None:
        return 0
    rows = fed.read_conn.execute(
        "SELECT asset_id, _root, rel_path FROM all_assets"
        " WHERE has_mask = 0 AND exists_flag = 1 LIMIT ?",
        (limit,),
    ).fetchall()
    repaired = 0
    for r in rows:
        shard = fed.shards.get(r["_root"])
        if shard is None:
            continue
        abs_path = os.path.join(shard.abs_path, r["rel_path"])
        if os.path.isfile(imgdb.mask_path_for(abs_path)):
            imgdb.set_has_mask(shard.conn, r["asset_id"], True)
            repaired += 1
    return repaired


def count_assets_missing_perceptual_hash(fed: Federation) -> int:
    """Total existing assets with no perceptual hash."""
    if fed.read_conn is None:
        return 0
    return fed.read_conn.execute(
        "SELECT COUNT(*) FROM all_assets"
        " WHERE perceptual_hash IS NULL AND exists_flag = 1"
    ).fetchone()[0]


def list_assets_missing_perceptual_hash(
    fed: Federation,
    limit: int,
    priority_labels: Optional[list[str]] = None,
    priority_rules: Optional[list[FilterRule]] = None,
) -> list[tuple[str, str, str]]:
    """
    Return up to `limit` (asset_id, rel_path, shard_label) tuples for
    existing assets whose perceptual_hash is NULL.

    When priority_labels/priority_rules are given, assets matching the
    current filter are returned first; remaining slots are filled from all
    assets so the backfill eventually completes even when the filter is
    narrow.
    """
    if fed.read_conn is None:
        return []

    result: list[tuple[str, str, str]] = []

    if priority_labels is not None or priority_rules:
        conds, params = build_filter_conditions(
            priority_rules or [], priority_labels, alias="a"
        )
        if conds != ["1=0"]:
            conds += ["a.perceptual_hash IS NULL", "a.exists_flag = 1"]
            sql = (
                "SELECT a.asset_id, a._root, a.rel_path FROM all_assets a"
                " WHERE " + " AND ".join(conds) + " LIMIT ?"
            )
            params.append(limit)
            for r in fed.read_conn.execute(sql, params).fetchall():
                result.append((r["asset_id"], r["rel_path"], r["_root"]))

    remaining = limit - len(result)
    if remaining > 0:
        seen_ids = {r[0] for r in result}
        if seen_ids:
            placeholders = ",".join("?" * len(seen_ids))
            sql = (
                "SELECT asset_id, _root, rel_path FROM all_assets"
                f" WHERE perceptual_hash IS NULL AND exists_flag = 1"
                f" AND asset_id NOT IN ({placeholders}) LIMIT ?"
            )
            params = [*seen_ids, remaining]
        else:
            sql = (
                "SELECT asset_id, _root, rel_path FROM all_assets"
                " WHERE perceptual_hash IS NULL AND exists_flag = 1 LIMIT ?"
            )
            params = [remaining]
        for r in fed.read_conn.execute(sql, params).fetchall():
            result.append((r["asset_id"], r["rel_path"], r["_root"]))

    return result


def run_user_query(
    fed: Federation, sql: str
) -> tuple[list[str], list[tuple]]:
    """
    Execute a user-supplied SELECT on the read connection. The read
    connection has every shard attached as its label and has `all_*`
    union views available.
    """
    if fed.read_conn is None:
        raise FederationError(
            "No shards are attached; cannot run queries. Attach a root first."
        )
    cur = fed.read_conn.execute(sql)
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchall()
    return cols, [tuple(r) for r in rows]


def search_captions(
    fed: Federation, match_expr: str
) -> list[tuple[str, str, str, str]]:
    """
    Fan out an FTS5 MATCH query across every attached shard. Returns
    (shard_label, asset_id, kind, content) tuples. FTS5 virtual tables
    can't be unioned via views, so this is a function, not a view.
    """
    results: list[tuple[str, str, str, str]] = []
    for label, shard in fed.shards.items():
        for asset_id, kind, content in imgdb.search_captions(shard.conn, match_expr):
            results.append((label, asset_id, kind, content))
    return results


# ---------------------------------------------------------------------------
# Bulk import from paired image/.txt files
# ---------------------------------------------------------------------------

@dataclass
class BulkImportSummary:
    processed: int = 0       # txt content successfully imported
    skipped: int = 0         # skipped — caption already exists, no overwrite
    no_txt: int = 0          # image found but no paired .txt file
    not_registered: int = 0  # image not in DB (metadata_only mode only)
    copied: int = 0          # files copied/moved to root
    errors: list = field(default_factory=list)


def bulk_import_paired_files(
    fed: Federation,
    source_dir: str,
    shard_label: str,
    caption_kind: Optional[str],
    overwrite: bool = True,
    file_mode: str = "metadata_only",
    dest_subdir: str = "",
    on_event: Optional[Callable[[str, str], None]] = None,
) -> BulkImportSummary:
    """
    Import images and their paired .txt metadata from source_dir.

    file_mode:
        "metadata_only" — source_dir is inside the shard root; images must
                          already be registered (a scan is run first to ensure
                          any unregistered images are picked up).
        "copy"          — copy image files from source_dir into the shard root,
                          then import their metadata.
        "move"          — move image files from source_dir into the shard root,
                          then import their metadata.

    caption_kind:
        If not None, .txt content is stored as a caption of this kind.
        If None, .txt content is parsed as comma-separated tags.

    overwrite:
        If True, existing captions of the same kind are replaced.
        If False, existing captions are left as-is and counted as skipped.
        (Tags are always additive — duplicates are ignored regardless.)

    dest_subdir:
        Only used in copy/move mode. Path relative to the shard root under
        which imported images are placed, preserving subdirectory structure.
        Empty string places them directly under the shard root.
    """
    shard = shard_by_label(fed, shard_label)
    summary = BulkImportSummary()

    source_dir = os.path.abspath(source_dir)
    root_abs = shard.abs_path
    # Resolve symlinks so that paths navigated via different mount points or
    # symlinks still compare correctly against the registered root path.
    real_root = os.path.realpath(root_abs)

    # Collect (src_abs, dest_rel, txt_abs_or_None) for every image in source_dir.
    img_pairs: list[tuple[str, str, Optional[str]]] = []
    for dirpath, _dirs, filenames in os.walk(source_dir):
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext not in imgdb.DEFAULT_EXTENSIONS:
                continue
            src_abs = os.path.join(dirpath, name)
            txt_abs_candidate = os.path.splitext(src_abs)[0] + ".txt"
            txt_abs = txt_abs_candidate if os.path.isfile(txt_abs_candidate) else None

            if file_mode == "metadata_only":
                real_src = os.path.realpath(src_abs)
                rel = os.path.relpath(real_src, real_root).replace(os.sep, "/")
                if rel.startswith("../"):
                    summary.errors.append(
                        f"Skipping {name!r}: not inside root "
                        f"(file={real_src!r}, root={real_root!r})"
                    )
                    continue
            else:
                rel_from_source = os.path.relpath(src_abs, source_dir).replace(os.sep, "/")
                rel = (dest_subdir.strip("/") + "/" + rel_from_source).lstrip("/") \
                    if dest_subdir.strip("/") else rel_from_source

            img_pairs.append((src_abs, rel, txt_abs))

    # Copy or move images to the shard root before registering.
    if file_mode in ("copy", "move"):
        for src_abs, dest_rel, _txt in img_pairs:
            dest_abs = os.path.join(root_abs, dest_rel.replace("/", os.sep))
            if os.path.abspath(src_abs) == os.path.abspath(dest_abs):
                continue
            os.makedirs(os.path.dirname(dest_abs), exist_ok=True)
            try:
                if file_mode == "copy":
                    shutil.copy2(src_abs, dest_abs)
                else:
                    shutil.move(src_abs, dest_abs)
                summary.copied += 1
                if on_event:
                    on_event("copying", dest_rel)
            except OSError as e:
                summary.errors.append(f"Failed to {file_mode} {src_abs!r}: {e}")

    # Scan the shard to register any new or moved images.
    scan_shard(fed, shard_label, on_event=on_event)

    # Import metadata for each pair.
    for _src_abs, dest_rel, txt_abs in img_pairs:
        if on_event:
            on_event("importing", dest_rel)

        if txt_abs is None:
            summary.no_txt += 1
            continue

        row = shard.conn.execute(
            "SELECT asset_id FROM assets WHERE rel_path = ?", (dest_rel,)
        ).fetchone()
        if row is None:
            summary.not_registered += 1
            continue
        asset_id = row["asset_id"]

        try:
            content = open(txt_abs, encoding="utf-8", errors="replace").read().strip()
        except OSError as e:
            summary.errors.append(f"Cannot read {txt_abs!r}: {e}")
            continue

        if not content:
            summary.no_txt += 1
            continue

        try:
            if caption_kind is not None:
                if not overwrite:
                    exists = shard.conn.execute(
                        "SELECT 1 FROM captions WHERE asset_id = ? AND kind = ?",
                        (asset_id, caption_kind),
                    ).fetchone()
                    if exists:
                        summary.skipped += 1
                        continue
                imgdb.set_caption(shard.conn, asset_id, caption_kind, content)
            else:
                tags = [
                    t.strip()
                    for t in content.replace("\n", ",").split(",")
                    if t.strip()
                ]
                if tags:
                    imgdb.add_tags(shard.conn, asset_id, tags)
            summary.processed += 1
        except Exception as e:
            summary.errors.append(f"Error on {dest_rel!r}: {e}")

    return summary


# ---------------------------------------------------------------------------
# Datasets (federation level)
# ---------------------------------------------------------------------------

def add_to_dataset(
    fed: Federation,
    name: str,
    asset_ids: Iterable[str],
    description: str = "",
) -> None:
    """
    Add assets to a named dataset, routing each asset_id to its owning shard.
    The dataset row is created in each shard lazily on first use.
    """
    by_shard: dict[str, list[str]] = {}
    for asset_id in list(asset_ids):
        shard = shard_for_asset(fed, asset_id)
        by_shard.setdefault(shard.label, []).append(asset_id)
    for label, ids in by_shard.items():
        imgdb.add_to_dataset(fed.shards[label].conn, name, ids, description)


def add_to_dataset_from_query(
    fed: Federation,
    name: str,
    sql: str,
    description: str = "",
) -> int:
    """
    Execute sql (must SELECT an asset_id column), add all results to the
    named dataset. Returns the number of asset_ids processed.
    """
    cols, rows = run_user_query(fed, sql)
    col_lower = [c.lower() for c in cols]
    if "asset_id" not in col_lower:
        raise FederationError("Query must return an 'asset_id' column")
    idx = col_lower.index("asset_id")
    asset_ids = [str(r[idx]) for r in rows if r[idx] is not None]
    if asset_ids:
        add_to_dataset(fed, name, asset_ids, description)
    return len(asset_ids)


def remove_from_dataset(
    fed: Federation,
    name: str,
    asset_ids: Iterable[str],
) -> None:
    """Remove assets from a dataset, routing each to its owning shard."""
    by_shard: dict[str, list[str]] = {}
    for asset_id in list(asset_ids):
        label = fed.asset_index.get(asset_id)
        if label and label in fed.shards:
            by_shard.setdefault(label, []).append(asset_id)
    for label, ids in by_shard.items():
        imgdb.remove_from_dataset(fed.shards[label].conn, name, ids)


def rename_dataset(fed: Federation, old_name: str, new_name: str) -> None:
    """Rename a dataset across all shards that contain it."""
    for shard in fed.shards.values():
        row = shard.conn.execute(
            "SELECT 1 FROM datasets WHERE name = ?", (old_name,)
        ).fetchone()
        if row:
            imgdb.rename_dataset(shard.conn, old_name, new_name)


def delete_dataset(fed: Federation, name: str) -> list[str]:
    """
    Remove a dataset from all currently-attached shards that contain it.
    Returns the list of shard labels that were affected. Shards that are
    offline are not touched and will still hold the dataset.
    """
    affected: list[str] = []
    for label, shard in fed.shards.items():
        row = shard.conn.execute(
            "SELECT 1 FROM datasets WHERE name = ?", (name,)
        ).fetchone()
        if row:
            imgdb.delete_dataset(shard.conn, name)
            affected.append(label)
    return affected


def list_all_tags_with_counts(fed: Federation) -> list[tuple[str, str, int]]:
    """
    Return (tag_name, type_name, usage_count) across all attached shards,
    sorted by count descending. Carrying type alongside count lets callers
    filter suggestions by category without a second query.
    """
    if fed.read_conn is None:
        return []
    rows = fed.read_conn.execute(
        """
        SELECT t.name, tt.name AS type_name, COUNT(at.asset_id) AS cnt
          FROM all_tags t
          JOIN all_tag_types tt ON tt.type_id = t.type_id AND tt._root = t._root
          JOIN all_asset_tags at ON at.tag_id = t.tag_id AND at._root = t._root
         GROUP BY t.name, tt.name
         ORDER BY cnt DESC, t.name ASC
        """
    ).fetchall()
    return [(row[0], row[1], row[2]) for row in rows]


def build_tag_lookup(
    fed: Federation,
) -> dict[str, tuple[str, list[str]]]:
    """
    Return {lowercase_name: (canonical_name, [type_name, ...])} for every tag
    known across all attached shards.

    A tag name that appears under different categories (even across shards) is
    considered ambiguous; its types list will contain more than one entry.
    """
    if fed.read_conn is None:
        return {}
    rows = fed.read_conn.execute(
        "SELECT t.name, tt.name AS type_name"
        "  FROM all_tags t"
        "  JOIN all_tag_types tt ON tt.type_id = t.type_id AND tt._root = t._root"
        " GROUP BY LOWER(t.name), tt.name"
        " ORDER BY LOWER(t.name)"
    ).fetchall()
    lookup: dict[str, tuple[str, list[str]]] = {}
    for row in rows:
        key = row["name"].lower()
        type_name = row["type_name"]
        if key in lookup:
            if type_name not in lookup[key][1]:
                lookup[key][1].append(type_name)
        else:
            lookup[key] = (row["name"], [type_name])
    return lookup


def match_tags_in_text(
    text: str,
    tag_lookup: dict[str, tuple[str, list[str]]],
) -> list[tuple[str, list[str]]]:
    """
    Find all known tags present as whole-word phrases in text.
    Returns [(canonical_name, [type_names])] in first-appearance order.

    Shorter tags that are sub-phrases of longer matched tags are suppressed
    unless they also occur independently at a position not covered by any
    longer match.

    Example: in "dramatic lighting", "dramatic" is suppressed because its
    only occurrence is covered by "dramatic lighting".  In "dramatic lighting
    and dramatic shadows", "dramatic" is kept because its second occurrence
    at position 3 is not covered by anything.
    """
    tokens = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_]*", text.lower())
    if not tokens or not tag_lookup:
        return []
    max_n = min(max(len(k.split()) for k in tag_lookup), 6)
    n_tokens = len(tokens)

    # Collect every (start, end) span where each key matches.
    all_spans: dict[str, list[tuple[int, int]]] = {}
    for i in range(n_tokens):
        for n in range(1, min(max_n, n_tokens - i) + 1):
            ngram = " ".join(tokens[i : i + n])
            if ngram in tag_lookup:
                all_spans.setdefault(ngram, []).append((i, i + n))

    if not all_spans:
        return []

    # Flat list of all match spans used for coverage checks.
    flat: list[tuple[int, int]] = [s for spans in all_spans.values() for s in spans]

    # A tag is kept only if at least one of its spans is not fully covered
    # by some strictly longer match span.  Record the position of the first
    # such uncovered span for ordering.
    first_pos: dict[str, int] = {}
    for key, spans in all_spans.items():
        for start, end in spans:
            length = end - start
            if not any(a <= start and b >= end and (b - a) > length for a, b in flat):
                first_pos.setdefault(key, start)

    return [
        (tag_lookup[k][0], list(tag_lookup[k][1]))
        for k in sorted(first_pos, key=first_pos.__getitem__)
    ]


def list_all_caption_kinds(
    fed: Federation,
    checked_labels: Optional[Iterable[str]] = None,
) -> list[str]:
    """Return distinct caption kinds present in the checked shards."""
    if fed.read_conn is None:
        return []
    conditions: list[str] = []
    params: list = []
    if checked_labels is not None:
        labels = list(checked_labels)
        if not labels:
            return []
        ph = ",".join("?" * len(labels))
        conditions.append(f"a._root IN ({ph})")
        params.extend(labels)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    rows = fed.read_conn.execute(
        f"SELECT DISTINCT c.kind"
        f"  FROM all_captions c"
        f"  JOIN all_assets a ON a.asset_id = c.asset_id AND a._root = c._root"
        f" {where}"
        f" ORDER BY c.kind",
        params,
    ).fetchall()
    return [r["kind"] for r in rows]


def _caption_text_for_asset(
    shard: "Shard",
    asset_id: str,
    caption_kind: Optional[str],
) -> str:
    """Fetch caption text for one asset. None caption_kind concatenates all kinds."""
    if caption_kind is None:
        rows = shard.conn.execute(
            "SELECT content FROM captions WHERE asset_id = ? ORDER BY kind",
            (asset_id,),
        ).fetchall()
        return " ".join(r["content"] for r in rows if r["content"])
    row = shard.conn.execute(
        "SELECT content FROM captions WHERE asset_id = ? AND kind = ?",
        (asset_id, caption_kind),
    ).fetchone()
    return (row["content"] or "") if row else ""


def import_caption_tags_for_asset(
    fed: Federation,
    asset_id: str,
    caption_kind: Optional[str],
    tag_lookup: dict[str, tuple[str, list[str]]],
    resolution: dict[str, str],
    only_names: Optional[set[str]] = None,
    _shard: Optional["Shard"] = None,
) -> int:
    """
    Match tags in an asset's caption(s) and add any not already present.

    resolution: {lowercase_name: type_name} for ambiguous tags; names absent
                from resolution default to "General".
    only_names: if provided, restrict to this lowercase name set (single-image
                checklist).
    _shard: optional pre-resolved Shard; if omitted, resolved via asset_index.
    Returns the count of tag assignments added.  add_tags uses INSERT OR IGNORE
    so existing assignments are silently skipped.
    """
    shard = _shard if _shard is not None else shard_for_asset(fed, asset_id)
    text = _caption_text_for_asset(shard, asset_id, caption_kind)
    if not text.strip():
        return 0
    count = 0
    for canon, types in match_tags_in_text(text, tag_lookup):
        if only_names is not None and canon.lower() not in only_names:
            continue
        type_name = types[0] if len(types) == 1 else resolution.get(canon.lower(), "General")
        add_tags(fed, asset_id, [canon], type_name=type_name)
        count += 1
    return count


def prescan_ambiguous_matches(
    fed: Federation,
    caption_kind: Optional[str],
    tag_lookup: dict[str, tuple[str, list[str]]],
    checked_labels: Optional[list[str]] = None,
    filter_rules: Optional[list[FilterRule]] = None,
) -> list[tuple[str, list[str]]]:
    """
    Scan all in-scope captions and return the unique set of ambiguous tag
    matches (those with more than one possible category).  Used before bulk
    "Ask per tag" imports so the user resolves each name exactly once.
    """
    seen: dict[str, tuple[str, list[str]]] = {}
    for asset in list_filtered_assets(fed, checked_labels, filter_rules or [], []):
        shard = fed.shards.get(asset.root)
        if shard is None:
            continue
        text = _caption_text_for_asset(shard, asset.asset_id, caption_kind)
        for canon, types in match_tags_in_text(text, tag_lookup):
            if len(types) > 1:
                key = canon.lower()
                if key not in seen:
                    seen[key] = (canon, list(types))
    return list(seen.values())


def bulk_import_caption_tags(
    fed: Federation,
    caption_kind: Optional[str],
    tag_lookup: dict[str, tuple[str, list[str]]],
    resolution: dict[str, str],
    checked_labels: Optional[list[str]] = None,
    filter_rules: Optional[list[FilterRule]] = None,
) -> int:
    """Import matched tags for every asset in scope. Returns total assignments added."""
    total = 0
    for asset in list_filtered_assets(
        fed,
        checked_labels,
        filter_rules or [],
        [],
    ):
        shard = fed.shards.get(asset.root)
        if shard is None:
            continue
        total += import_caption_tags_for_asset(
            fed, asset.asset_id, caption_kind, tag_lookup, resolution, _shard=shard
        )
    return total


def list_datasets_federation(fed: Federation) -> list[DatasetInfo]:
    """
    Merge dataset info across all currently-attached shards. A dataset that
    exists in multiple shards appears once with per-shard member counts summed.
    """
    merged: dict[str, dict] = {}
    for label, shard in fed.shards.items():
        for name, desc, count in imgdb.list_datasets(shard.conn):
            if name not in merged:
                merged[name] = {"description": desc, "shards": {}}
            merged[name]["shards"][label] = count
    return [
        DatasetInfo(
            name=name,
            description=info["description"],
            total_count=sum(info["shards"].values()),
            shard_counts=dict(info["shards"]),
        )
        for name, info in sorted(merged.items())
    ]
