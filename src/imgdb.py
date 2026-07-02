"""
imgdb — per-shard image catalog library.

This module operates on a single shard (one SQLite file belonging to one
root directory). It knows nothing about other shards, the federation layer,
argparse, stdout, or Tkinter. It returns data and raises typed exceptions.

Design invariants:
    - One file on disk = one asset. rel_path is UNIQUE within a shard.
    - asset_id is a stable UUID. file_hash is a mutable attribute that
      changes when a file is edited in place.
    - Shards are self-contained. A shard file does not record its own label
      or its own absolute path; those come from the config at runtime.
    - Destructive disk operations happen inside a DB transaction and commit
      only if the disk operation succeeded.
    - Derived data (thumbnails, etc.) lives at <root>/imgdb_thumbs/. Never
      outside the root.
    - No hidden files or directories anywhere.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

try:
    from blake3 import blake3
except ImportError as e:
    raise ImportError(
        "imgdb requires the 'blake3' package. Install with: pip install blake3"
    ) from e

try:
    from PIL import Image
except ImportError as e:
    raise ImportError(
        "imgdb requires the 'Pillow' package. Install with: pip install Pillow"
    ) from e

try:
    import imagehash as _imagehash
except ImportError as e:
    raise ImportError(
        "imgdb requires the 'imagehash' package. Install with: pip install imagehash"
    ) from e


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_EXTENSIONS: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".tif",
})

# File stems ending with any of these suffixes are silently skipped by the
# scanner — they are convention-based derivative/metadata files, not true
# image assets. Add entries here to exclude additional naming conventions.
EXCLUDED_STEM_SUFFIXES: tuple[str, ...] = (
    "_mask",       # segmentation/alpha masks
    "-masklabel",  # mask variant used by some labelling tools
    "_crop",       # cropped variants
)

THUMBS_DIRNAME = "imgdb_thumbs"
SHARD_DB_FILENAME = "imgdb.sqlite"
HASH_CHUNK_BYTES = 1024 * 1024  # 1 MiB

# Marker embedded in the sibling staging filename used by atomic disk ops
# (rename/delete). A crash between DB commit and finalize orphans one of these;
# scan_root surfaces them as "stale_staging" events for the user to clean up.
STAGING_MARKER = ".imgdb-tmp-"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ImgDBError(Exception):
    """Base class for all imgdb errors."""


class AssetNotFoundError(ImgDBError):
    pass


class TagNotFoundError(ImgDBError):
    pass


class CaptionNotFoundError(ImgDBError):
    pass


class MergeError(ImgDBError):
    pass


class DatasetError(ImgDBError):
    pass


class FileOperationError(ImgDBError):
    """Raised when a disk operation fails during a DB-coordinated op."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Asset:
    asset_id: str
    rel_path: str
    file_hash: str
    perceptual_hash: Optional[str]
    width: Optional[int]
    height: Optional[int]
    format: Optional[str]
    bytes: Optional[int]


@dataclass(frozen=True)
class ProbeResult:
    width: Optional[int]
    height: Optional[int]
    format: Optional[str]
    bytes: int


@dataclass
class ScanSummary:
    new: int = 0
    edited: int = 0
    unchanged: int = 0
    missing: int = 0


# ---------------------------------------------------------------------------
# Schema (per shard)
# ---------------------------------------------------------------------------

SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS assets (
    asset_id         TEXT PRIMARY KEY,
    rel_path         TEXT NOT NULL UNIQUE,
    file_hash        TEXT NOT NULL,
    perceptual_hash  TEXT,
    width            INTEGER,
    height           INTEGER,
    format           TEXT,
    bytes            INTEGER,
    mtime_ns         INTEGER,
    last_seen        TIMESTAMP,
    exists_flag      INTEGER DEFAULT 1,
    tags_validated   INTEGER NOT NULL DEFAULT 0,
    has_mask         INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_assets_hash  ON assets(file_hash);
CREATE INDEX IF NOT EXISTS idx_assets_phash ON assets(perceptual_hash);

CREATE TABLE IF NOT EXISTS asset_hash_history (
    asset_id     TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
    hash         TEXT NOT NULL,
    replaced_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (asset_id, hash)
);
CREATE INDEX IF NOT EXISTS idx_hash_history_hash ON asset_hash_history(hash);

CREATE TABLE IF NOT EXISTS captions (
    caption_id   INTEGER PRIMARY KEY,
    asset_id     TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    content      TEXT NOT NULL,
    is_validated INTEGER NOT NULL DEFAULT 0,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (asset_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_captions_asset ON captions(asset_id);

CREATE VIRTUAL TABLE IF NOT EXISTS captions_fts USING fts5(
    content_text,
    content='captions',
    content_rowid='caption_id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS captions_ai AFTER INSERT ON captions BEGIN
    INSERT INTO captions_fts(rowid, content_text) VALUES (new.caption_id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS captions_ad AFTER DELETE ON captions BEGIN
    INSERT INTO captions_fts(captions_fts, rowid, content_text)
    VALUES('delete', old.caption_id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS captions_au AFTER UPDATE ON captions BEGIN
    INSERT INTO captions_fts(captions_fts, rowid, content_text)
    VALUES('delete', old.caption_id, old.content);
    INSERT INTO captions_fts(rowid, content_text) VALUES (new.caption_id, new.content);
END;

CREATE TABLE IF NOT EXISTS tag_types (
    type_id    INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO tag_types(type_id, name) VALUES (0, 'General');

CREATE TABLE IF NOT EXISTS tags (
    tag_id  TEXT    PRIMARY KEY,
    name    TEXT    NOT NULL COLLATE NOCASE,
    type_id INTEGER NOT NULL DEFAULT 0
            REFERENCES tag_types(type_id) ON DELETE RESTRICT,
    UNIQUE(name, type_id)
);

CREATE TABLE IF NOT EXISTS asset_tags (
    asset_id     TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
    tag_id       TEXT NOT NULL REFERENCES tags(tag_id) ON DELETE CASCADE,
    PRIMARY KEY (asset_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_asset_tags_tag ON asset_tags(tag_id);

CREATE TABLE IF NOT EXISTS merged_assets (
    old_asset_id TEXT PRIMARY KEY,
    new_asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
    merged_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_merged_new ON merged_assets(new_asset_id);

CREATE TABLE IF NOT EXISTS datasets (
    name         TEXT PRIMARY KEY,
    description  TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dataset_assets (
    dataset_name TEXT NOT NULL REFERENCES datasets(name) ON DELETE CASCADE,
    asset_id     TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
    added_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (dataset_name, asset_id)
);
CREATE INDEX IF NOT EXISTS idx_dataset_assets_asset ON dataset_assets(asset_id);
CREATE INDEX IF NOT EXISTS idx_dataset_assets_name  ON dataset_assets(dataset_name);
"""


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

def shard_db_path(root_abs_path: str) -> str:
    """Return the absolute path to a root's shard database file."""
    return os.path.join(os.path.abspath(root_abs_path), SHARD_DB_FILENAME)


def connect(db_path: str | os.PathLike, read_only: bool = False) -> sqlite3.Connection:
    """
    Open a SQLite connection with the project's required PRAGMAs.

    `isolation_level=None` puts the connection in autocommit mode so we can
    drive transactions explicitly via the `transaction()` context manager.
    Without this, sqlite3's implicit BEGIN/COMMIT collides with explicit BEGIN.
    """
    db_path = str(db_path)
    if read_only:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, isolation_level=None)
    else:
        conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if not read_only:
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -65536")  # 64 MiB page cache
        conn.execute("PRAGMA mmap_size = 268435456")  # 256 MiB
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply incremental column additions/renames to existing shards."""
    asset_cols = {row[1] for row in conn.execute("PRAGMA table_info(assets)")}
    # Renames (SQLite 3.35+, safe on Python 3.10+).
    if "current_hash" in asset_cols and "file_hash" not in asset_cols:
        conn.execute("ALTER TABLE assets RENAME COLUMN current_hash TO file_hash")
    if "phash" in asset_cols and "perceptual_hash" not in asset_cols:
        conn.execute("ALTER TABLE assets RENAME COLUMN phash TO perceptual_hash")
    elif "perceptual_hash" not in asset_cols:
        conn.execute("ALTER TABLE assets ADD COLUMN perceptual_hash TEXT")
    # Refresh after renames.
    asset_cols = {row[1] for row in conn.execute("PRAGMA table_info(assets)")}
    if "tags_validated" not in asset_cols:
        conn.execute("ALTER TABLE assets ADD COLUMN tags_validated INTEGER NOT NULL DEFAULT 0")
    if "has_mask" not in asset_cols:
        conn.execute("ALTER TABLE assets ADD COLUMN has_mask INTEGER NOT NULL DEFAULT 0")
    caption_cols = {row[1] for row in conn.execute("PRAGMA table_info(captions)")}
    if "is_validated" not in caption_cols:
        conn.execute("ALTER TABLE captions ADD COLUMN is_validated INTEGER NOT NULL DEFAULT 0")


def init_shard(root_abs_path: str) -> sqlite3.Connection:
    """
    Open (creating if needed) a shard for the given root and apply the
    schema. Sets WAL mode (persistent property of the file). Returns the
    connection.
    """
    os.makedirs(root_abs_path, exist_ok=True)
    conn = connect(shard_db_path(root_abs_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA_SQL)
    _migrate(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """
    Explicit IMMEDIATE transaction with rollback on exception. Requires
    the connection to be in autocommit mode (see `connect()`).
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


# ---------------------------------------------------------------------------
# File utilities
# ---------------------------------------------------------------------------

def hash_file(path: str | os.PathLike) -> str:
    """BLAKE3 of a file, streamed in 1 MiB chunks. Constant memory."""
    hasher = blake3()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


# Sentinel stored in perceptual_hash for assets that could not be hashed.
# NULL means "not yet attempted"; PHASH_FAILED means "tried, but failed."
# All queries that look for un-hashed assets filter on IS NULL, so PHASH_FAILED
# is excluded automatically — those assets are never re-queued for backfill.
PHASH_FAILED = ""


def compute_perceptual_hash(path: str | os.PathLike) -> Optional[str]:
    """
    Return the imagehash pHash (hex string) of the image's pixel content.
    Returns None if the file cannot be opened as an image.
    Format, compression, and metadata are ignored — only pixel data matters.
    """
    try:
        with Image.open(path) as img:
            return str(_imagehash.phash(img))
    except Exception:
        return None


def set_perceptual_hash(
    conn: sqlite3.Connection, asset_id: str, phash: Optional[str]
) -> None:
    """Write the perceptual hash for an asset. Used by backfill and preview."""
    with transaction(conn):
        conn.execute(
            "UPDATE assets SET perceptual_hash = ? WHERE asset_id = ?",
            (phash, asset_id),
        )


def probe_image(path: str | os.PathLike) -> ProbeResult:
    """Extract dimensions, format, and size without loading pixels."""
    st = os.stat(path)
    try:
        with Image.open(path) as im:
            width, height = im.size
            fmt = im.format
    except Exception:
        return ProbeResult(width=None, height=None, format=None, bytes=st.st_size)
    return ProbeResult(width=width, height=height, format=fmt, bytes=st.st_size)


def thumbs_dir(root_abs_path: str) -> Path:
    """
    Derived-data directory for a root.

    INVARIANT: always inside the root's abs_path. Derived data is strictly
    compartmentalized to the root it describes.
    """
    return Path(root_abs_path) / THUMBS_DIRNAME


# ---------------------------------------------------------------------------
# Asset lookups
# ---------------------------------------------------------------------------

_ASSET_COLS = "asset_id, rel_path, file_hash, perceptual_hash, width, height, format, bytes"


def _row_to_asset(row: sqlite3.Row) -> Asset:
    return Asset(
        asset_id=row["asset_id"],
        rel_path=row["rel_path"],
        file_hash=row["file_hash"],
        perceptual_hash=row["perceptual_hash"],
        width=row["width"],
        height=row["height"],
        format=row["format"],
        bytes=row["bytes"],
    )


def get_asset(conn: sqlite3.Connection, asset_id: str) -> Asset:
    row = conn.execute(
        f"SELECT {_ASSET_COLS} FROM assets WHERE asset_id = ?", (asset_id,)
    ).fetchone()
    if row is None:
        raise AssetNotFoundError(f"No asset with id {asset_id!r} in this shard")
    return _row_to_asset(row)


def get_asset_by_rel_path(conn: sqlite3.Connection, rel_path: str) -> Optional[Asset]:
    """Return the asset stored at rel_path in this shard, or None."""
    row = conn.execute(
        f"SELECT {_ASSET_COLS} FROM assets WHERE rel_path = ?", (rel_path,)
    ).fetchone()
    return _row_to_asset(row) if row is not None else None


def list_asset_ids(conn: sqlite3.Connection) -> list[str]:
    """Every asset_id in this shard. Used by the federation index."""
    return [r["asset_id"] for r in conn.execute("SELECT asset_id FROM assets")]


def list_merged_ids(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """
    Every (old_asset_id, new_asset_id) pair in this shard's merge history.
    Used by the federation index so old IDs resolve across the federation.
    """
    return [
        (r["old_asset_id"], r["new_asset_id"])
        for r in conn.execute("SELECT old_asset_id, new_asset_id FROM merged_assets")
    ]


def resolve_merged_id(conn: sqlite3.Connection, asset_id: str) -> str:
    """
    Walk the merged_assets chain to the current asset_id for a given
    (possibly historical) id. Returns the input unchanged if it's current.
    """
    row = conn.execute(
        """
        WITH RECURSIVE resolve(id, depth) AS (
            SELECT ?, 0
            UNION ALL
            SELECT m.new_asset_id, r.depth + 1
            FROM merged_assets m JOIN resolve r ON m.old_asset_id = r.id
            WHERE r.depth < 64
        )
        SELECT id, depth FROM resolve ORDER BY depth DESC LIMIT 1
        """,
        (asset_id,),
    ).fetchone()
    if row is None:
        return asset_id
    if row["depth"] >= 64:
        raise MergeError(f"Merge chain for {asset_id!r} exceeds depth 64")
    return row["id"]


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def _iter_candidate_files(
    root_abs: str, extensions: frozenset[str]
) -> Iterator[tuple[str, str]]:
    """
    Yield (abs_path, rel_path) for each file under root_abs whose extension
    is in the whitelist. Skips the thumbnails directory and the shard DB
    files at the root level.
    """
    root_abs = os.path.abspath(root_abs)
    shard_skip = {
        SHARD_DB_FILENAME,
        SHARD_DB_FILENAME + "-wal",
        SHARD_DB_FILENAME + "-shm",
    }
    for dirpath, dirnames, filenames in os.walk(root_abs):
        if os.path.abspath(dirpath) == root_abs and THUMBS_DIRNAME in dirnames:
            dirnames.remove(THUMBS_DIRNAME)
        for name in filenames:
            if os.path.abspath(dirpath) == root_abs and name in shard_skip:
                continue
            stem, ext = os.path.splitext(name)
            ext = ext.lower()
            if ext not in extensions:
                continue
            if any(stem.endswith(suffix) for suffix in EXCLUDED_STEM_SUFFIXES):
                continue
            abs_path = os.path.join(dirpath, name)
            rel_path = os.path.relpath(abs_path, root_abs).replace(os.sep, "/")
            yield abs_path, rel_path


def _iter_stale_staging(root_abs: str) -> Iterator[str]:
    """
    Yield rel_path for each orphaned ``.imgdb-tmp-*`` staging file under
    root_abs (skipping the thumbnails directory). These are left behind only
    by a crash between DB commit and finalize; there is no legitimate reason
    for one to persist across a scan.
    """
    root_abs = os.path.abspath(root_abs)
    for dirpath, dirnames, filenames in os.walk(root_abs):
        if os.path.abspath(dirpath) == root_abs and THUMBS_DIRNAME in dirnames:
            dirnames.remove(THUMBS_DIRNAME)
        for name in filenames:
            if STAGING_MARKER in name:
                abs_path = os.path.join(dirpath, name)
                yield os.path.relpath(abs_path, root_abs).replace(os.sep, "/")


SCAN_BATCH_SIZE = 500


def scan_root(
    conn: sqlite3.Connection,
    root_abs_path: str,
    extensions: frozenset[str] = DEFAULT_EXTENSIONS,
    on_event: Optional[Callable[[str, str], None]] = None,
    cancel: Optional[threading.Event] = None,
) -> tuple[ScanSummary, list[str]]:
    """
    Walk `root_abs_path`, ingest files into this shard, and mark missing
    files (previously known but no longer on disk).

    Returns (summary, new_asset_ids). Commits in batches of SCAN_BATCH_SIZE
    so progress is durable and the WAL doesn't grow without bound.
    """
    summary = ScanSummary()
    new_ids: list[str] = []

    with transaction(conn):
        conn.execute(
            "CREATE TEMP TABLE _seen_paths (rel_path TEXT PRIMARY KEY) WITHOUT ROWID"
        )

    pending = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for abs_path, rel_path in _iter_candidate_files(root_abs_path, extensions):
            if cancel is not None and cancel.is_set():
                break
            try:
                kind, new_id = _ingest_file(conn, abs_path, rel_path)
            except Exception as e:
                conn.rollback()
                raise FileOperationError(f"Failed to ingest {rel_path!r}: {e}") from e
            conn.execute(
                "INSERT OR IGNORE INTO _seen_paths(rel_path) VALUES (?)", (rel_path,)
            )
            setattr(summary, kind, getattr(summary, kind) + 1)
            if new_id is not None:
                new_ids.append(new_id)
            if on_event:
                on_event(kind, rel_path)
            pending += 1
            if pending >= SCAN_BATCH_SIZE:
                conn.commit()
                conn.execute("BEGIN IMMEDIATE")
                pending = 0

        # Mark missing files via set-difference in SQL.
        if on_event:
            missing_rows = conn.execute(
                """
                SELECT rel_path FROM assets
                 WHERE exists_flag = 1
                   AND rel_path NOT IN (SELECT rel_path FROM _seen_paths)
                """
            ).fetchall()
            for row in missing_rows:
                on_event("missing", row["rel_path"])
            for rel_path in _iter_stale_staging(root_abs_path):
                on_event("stale_staging", rel_path)

        for suffix in EXCLUDED_STEM_SUFFIXES:
            conn.execute(
                "DELETE FROM assets WHERE rel_path GLOB ?", (f"*{suffix}.*",)
            )

        cur = conn.execute(
            """
            UPDATE assets SET exists_flag = 0
             WHERE exists_flag = 1
               AND rel_path NOT IN (SELECT rel_path FROM _seen_paths)
            """
        )
        summary.missing = cur.rowcount or 0
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        try:
            conn.execute("DROP TABLE IF EXISTS _seen_paths")
        except sqlite3.Error:
            pass

    return summary, new_ids


# ---------------------------------------------------------------------------
# Incremental / interruptible scan  (used by the UI for large roots)
# ---------------------------------------------------------------------------

SCAN_BATCH_SIZE_INCREMENTAL = 20    # files per background job batch


@dataclass
class ScanSession:
    """
    Opaque state carried between the three phases of an incremental scan:
    scan_root_init → scan_root_batch (×N) → scan_root_finish.

    Created on the worker thread, passed back to the GUI thread as an opaque
    handle, then handed to each subsequent batch job.  Never touched from two
    threads at the same time.
    """
    root_abs_path: str
    all_paths: list          # list[tuple[str, str]] — (abs_path, rel_path)
    offset: int = field(default=0)
    summary: ScanSummary = field(default_factory=ScanSummary)
    new_ids: list = field(default_factory=list)


def scan_root_init(
    conn: sqlite3.Connection,
    root_abs_path: str,
    extensions: frozenset[str] = DEFAULT_EXTENSIONS,
) -> ScanSession:
    """
    Phase 1: create the seen-paths temp table and enumerate all candidate
    files.  Does NOT process any files.  Fast — just an os.walk.
    """
    # Drop any leftover table from a previously interrupted scan.
    conn.execute("DROP TABLE IF EXISTS _seen_paths")
    with transaction(conn):
        conn.execute(
            "CREATE TEMP TABLE _seen_paths "
            "(rel_path TEXT PRIMARY KEY) WITHOUT ROWID"
        )
    paths = list(_iter_candidate_files(root_abs_path, extensions))
    return ScanSession(root_abs_path=root_abs_path, all_paths=paths)


def scan_root_batch(
    conn: sqlite3.Connection,
    session: ScanSession,
    on_event: Optional[Callable[[str, str], None]] = None,
    cancel: Optional[threading.Event] = None,
) -> bool:
    """
    Phase 2: process the next SCAN_BATCH_SIZE_INCREMENTAL files from the
    session.  Returns True when all files have been visited (or cancel was
    set).  Modifies session in-place.
    """
    batch = session.all_paths[
        session.offset : session.offset + SCAN_BATCH_SIZE_INCREMENTAL
    ]
    if not batch:
        return True

    processed = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for abs_path, rel_path in batch:
            if cancel is not None and cancel.is_set():
                break
            kind, new_id = _ingest_file(conn, abs_path, rel_path)
            conn.execute(
                "INSERT OR IGNORE INTO _seen_paths(rel_path) VALUES (?)", (rel_path,)
            )
            setattr(session.summary, kind, getattr(session.summary, kind) + 1)
            if new_id is not None:
                session.new_ids.append(new_id)
            if on_event:
                on_event(kind, rel_path)
            processed += 1
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    session.offset += processed
    cancelled = cancel is not None and cancel.is_set()
    return session.offset >= len(session.all_paths) or cancelled


def scan_root_finish(
    conn: sqlite3.Connection,
    session: ScanSession,
    on_event: Optional[Callable[[str, str], None]] = None,
) -> tuple[ScanSummary, list]:
    """
    Phase 3: mark files that disappeared since the last scan as missing, drop
    the temp table, and return (summary, new_ids).
    """
    try:
        if on_event:
            missing_rows = conn.execute(
                """
                SELECT rel_path FROM assets
                 WHERE exists_flag = 1
                   AND rel_path NOT IN (SELECT rel_path FROM _seen_paths)
                """
            ).fetchall()
            for row in missing_rows:
                on_event("missing", row["rel_path"])
            for rel_path in _iter_stale_staging(session.root_abs_path):
                on_event("stale_staging", rel_path)

        conn.execute("BEGIN IMMEDIATE")
        try:
            # Purge records for intentionally-excluded files (masks, crops, etc.)
            # that may have been ingested before the exclusion list was in place.
            # Delete rather than mark missing so they never reappear.
            for suffix in EXCLUDED_STEM_SUFFIXES:
                conn.execute(
                    "DELETE FROM assets WHERE rel_path GLOB ?", (f"*{suffix}.*",)
                )

            cur = conn.execute(
                """
                UPDATE assets SET exists_flag = 0
                 WHERE exists_flag = 1
                   AND rel_path NOT IN (SELECT rel_path FROM _seen_paths)
                """
            )
            session.summary.missing = cur.rowcount or 0
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        try:
            conn.execute("DROP TABLE IF EXISTS _seen_paths")
        except sqlite3.Error:
            pass

    return session.summary, session.new_ids


def _ingest_file(
    conn: sqlite3.Connection, abs_path: str, rel_path: str
) -> tuple[str, Optional[str]]:
    """
    Classify and ingest one file. Does NOT commit. Returns (event_kind, new_id).
    new_id is set only when a new asset row is created.

    Uses stat (size + mtime_ns) to skip rehashing files that haven't changed.
    """
    st = os.stat(abs_path)
    size = st.st_size
    mtime_ns = st.st_mtime_ns

    existing = conn.execute(
        "SELECT asset_id, file_hash, bytes, mtime_ns, has_mask FROM assets WHERE rel_path = ?",
        (rel_path,),
    ).fetchone()

    has_mask_on_disk = 1 if os.path.isfile(mask_path_for(abs_path)) else 0

    if (
        existing is not None
        and existing["bytes"] == size
        and existing["mtime_ns"] == mtime_ns
    ):
        if existing["has_mask"] != has_mask_on_disk:
            conn.execute(
                "UPDATE assets SET last_seen = CURRENT_TIMESTAMP, exists_flag = 1,"
                " has_mask = ? WHERE asset_id = ?",
                (has_mask_on_disk, existing["asset_id"]),
            )
        else:
            conn.execute(
                "UPDATE assets SET last_seen = CURRENT_TIMESTAMP, exists_flag = 1 "
                "WHERE asset_id = ?",
                (existing["asset_id"],),
            )
        return "unchanged", None

    content_hash = hash_file(abs_path)
    probe = probe_image(abs_path)

    if existing is not None:
        if existing["file_hash"] == content_hash:
            # Touched but content identical: refresh stat fields only.
            conn.execute(
                """
                UPDATE assets
                   SET bytes = ?, mtime_ns = ?, has_mask = ?,
                       last_seen = CURRENT_TIMESTAMP, exists_flag = 1
                 WHERE asset_id = ?
                """,
                (size, mtime_ns, has_mask_on_disk, existing["asset_id"]),
            )
            return "unchanged", None

        conn.execute(
            "INSERT OR IGNORE INTO asset_hash_history(asset_id, hash) VALUES (?, ?)",
            (existing["asset_id"], existing["file_hash"]),
        )
        phash = compute_perceptual_hash(abs_path)
        conn.execute(
            """
            UPDATE assets
               SET file_hash = ?, perceptual_hash = ?,
                   width = ?, height = ?, format = ?,
                   bytes = ?, mtime_ns = ?, has_mask = ?,
                   last_seen = CURRENT_TIMESTAMP, exists_flag = 1,
                   updated_at = CURRENT_TIMESTAMP
             WHERE asset_id = ?
            """,
            (
                content_hash, phash,
                probe.width, probe.height, probe.format,
                size, mtime_ns, has_mask_on_disk, existing["asset_id"],
            ),
        )
        return "edited", None

    phash = compute_perceptual_hash(abs_path)
    asset_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO assets(
            asset_id, rel_path, file_hash, perceptual_hash,
            width, height, format, bytes,
            mtime_ns, last_seen, exists_flag, has_mask
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 1, ?)
        """,
        (
            asset_id, rel_path, content_hash, phash,
            probe.width, probe.height, probe.format, size, mtime_ns,
            has_mask_on_disk,
        ),
    )
    return "new", asset_id


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

def get_or_create_tag_type(conn: sqlite3.Connection, name: str) -> int:
    """Look up or create a tag type by name. Returns local type_id."""
    row = conn.execute(
        "INSERT INTO tag_types(name) VALUES (?)"
        " ON CONFLICT(name) DO UPDATE SET name = name"
        " RETURNING type_id",
        (name,),
    ).fetchone()
    return row["type_id"]


def list_tag_types(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """Return all tag types as (type_id, name) in creation order."""
    return [
        (r["type_id"], r["name"])
        for r in conn.execute(
            "SELECT type_id, name FROM tag_types ORDER BY type_id ASC"
        )
    ]


def get_or_create_tag(conn: sqlite3.Connection, name: str, type_name: str = "General") -> str:
    """Public: look up or create a tag by (name, type_name). Returns tag_id."""
    type_id = get_or_create_tag_type(conn, type_name)
    return _get_or_create_tag(conn, name, type_id)


def _get_or_create_tag(conn: sqlite3.Connection, name: str, type_id: int = 0) -> str:
    """Single-roundtrip upsert. Requires SQLite >= 3.35 (RETURNING)."""
    new_id = str(uuid.uuid4())
    row = conn.execute(
        """
        INSERT INTO tags(tag_id, name, type_id) VALUES (?, ?, ?)
        ON CONFLICT(name, type_id) DO UPDATE SET name = name
        RETURNING tag_id
        """,
        (new_id, name, type_id),
    ).fetchone()
    return row["tag_id"]


def add_tags(
    conn: sqlite3.Connection,
    asset_id: str,
    tag_names: Iterable[str],
    type_name: str = "General",
) -> None:
    asset_id = resolve_merged_id(conn, asset_id)
    get_asset(conn, asset_id)
    type_id = get_or_create_tag_type(conn, type_name)
    with transaction(conn):
        for name in tag_names:
            tag_id = _get_or_create_tag(conn, name, type_id)
            conn.execute(
                "INSERT OR IGNORE INTO asset_tags(asset_id, tag_id) VALUES (?, ?)",
                (asset_id, tag_id),
            )


def remove_tags(
    conn: sqlite3.Connection, asset_id: str, tag_ids: Iterable[str]
) -> None:
    """Remove asset-tag links by tag_id. Silently skips IDs not linked to the asset."""
    asset_id = resolve_merged_id(conn, asset_id)
    get_asset(conn, asset_id)
    with transaction(conn):
        for tag_id in tag_ids:
            conn.execute(
                "DELETE FROM asset_tags WHERE asset_id = ? AND tag_id = ?",
                (asset_id, tag_id),
            )


def remove_tags_by_name(
    conn: sqlite3.Connection,
    asset_id: str,
    tag_name: str,
    type_name: Optional[str] = None,
) -> None:
    """Remove asset-tag links by name. If type_name is given, limits to that type."""
    asset_id = resolve_merged_id(conn, asset_id)
    with transaction(conn):
        if type_name is None:
            conn.execute(
                "DELETE FROM asset_tags WHERE asset_id = ?"
                " AND tag_id IN (SELECT tag_id FROM tags WHERE name = ?)",
                (asset_id, tag_name),
            )
        else:
            conn.execute(
                """DELETE FROM asset_tags WHERE asset_id = ?
                   AND tag_id IN (
                       SELECT t.tag_id FROM tags t
                       JOIN tag_types tt ON t.type_id = tt.type_id
                       WHERE t.name = ? AND tt.name = ?
                   )""",
                (asset_id, tag_name, type_name),
            )


def get_tags_for_asset(
    conn: sqlite3.Connection, asset_id: str
) -> list[sqlite3.Row]:
    """Return all tags for an asset with tag_id, name, and type_name columns."""
    return conn.execute(
        """SELECT t.tag_id, t.name, tt.name AS type_name
             FROM tags t
             JOIN tag_types tt ON tt.type_id = t.type_id
             JOIN asset_tags at ON at.tag_id = t.tag_id
            WHERE at.asset_id = ?
            ORDER BY tt.type_id ASC, t.name ASC""",
        (asset_id,),
    ).fetchall()


def get_captions_for_asset(
    conn: sqlite3.Connection, asset_id: str
) -> dict[str, tuple[str, bool]]:
    """Return {kind: (content, is_validated)} for all captions belonging to an asset."""
    return {
        r["kind"]: (r["content"], bool(r["is_validated"]))
        for r in conn.execute(
            "SELECT kind, content, is_validated FROM captions WHERE asset_id = ? ORDER BY kind",
            (asset_id,),
        )
    }


def get_tags_validated(conn: sqlite3.Connection, asset_id: str) -> bool:
    """Return whether the tags for an asset have been validated."""
    row = conn.execute(
        "SELECT tags_validated FROM assets WHERE asset_id = ?", (asset_id,)
    ).fetchone()
    return bool(row["tags_validated"]) if row else False


def set_tags_validated(
    conn: sqlite3.Connection, asset_id: str, validated: bool
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE assets SET tags_validated = ? WHERE asset_id = ?",
            (1 if validated else 0, asset_id),
        )


def set_caption_validated(
    conn: sqlite3.Connection, asset_id: str, kind: str, validated: bool
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE captions SET is_validated = ? WHERE asset_id = ? AND kind = ?",
            (1 if validated else 0, asset_id, kind),
        )


def mask_path_for(abs_path: str | os.PathLike) -> str:
    """Return the conventional path for an image's companion mask file."""
    base, _ = os.path.splitext(str(abs_path))
    return f"{base}_mask.png"


def set_has_mask(conn: sqlite3.Connection, asset_id: str, has_mask: bool) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE assets SET has_mask = ? WHERE asset_id = ?",
            (1 if has_mask else 0, asset_id),
        )


def get_dataset_membership(
    conn: sqlite3.Connection, asset_id: str
) -> list[str]:
    """Return dataset names this asset belongs to, sorted."""
    return [
        r[0] for r in conn.execute(
            "SELECT dataset_name FROM dataset_assets WHERE asset_id = ? ORDER BY dataset_name",
            (asset_id,),
        )
    ]


# ---------------------------------------------------------------------------
# Captions
# ---------------------------------------------------------------------------

def set_caption(
    conn: sqlite3.Connection, asset_id: str, kind: str, content: str
) -> None:
    asset_id = resolve_merged_id(conn, asset_id)
    get_asset(conn, asset_id)
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO captions(asset_id, kind, content)
            VALUES (?, ?, ?)
            ON CONFLICT(asset_id, kind) DO UPDATE SET
                content = excluded.content,
                updated_at = CURRENT_TIMESTAMP
            """,
            (asset_id, kind, content),
        )


def delete_caption(conn: sqlite3.Connection, asset_id: str, kind: str) -> None:
    asset_id = resolve_merged_id(conn, asset_id)
    get_asset(conn, asset_id)
    with transaction(conn):
        cur = conn.execute(
            "DELETE FROM captions WHERE asset_id = ? AND kind = ?", (asset_id, kind)
        )
        if cur.rowcount == 0:
            raise CaptionNotFoundError(
                f"No caption of kind {kind!r} on asset {asset_id!r}"
            )


# ---------------------------------------------------------------------------
# Rename and delete (atomic disk + DB)
# ---------------------------------------------------------------------------

def rename_asset(
    conn: sqlite3.Connection,
    root_abs_path: str,
    asset_id: str,
    new_rel_path: str,
    force: bool = False,
) -> None:
    """
    Rename an asset's file on disk and in the DB.

    Strategy: stage the rename to a sibling temp name first, commit the DB
    update, then finalize the rename. If the final rename fails the DB and
    disk are reconciled by a compensating move-back. This closes the
    commit-after-fs-op window.

    Note: this is not crash-safe across power loss; for that use a journal.
    """
    asset_id = resolve_merged_id(conn, asset_id)
    asset = get_asset(conn, asset_id)

    old_abs = os.path.join(root_abs_path, asset.rel_path)
    new_abs = os.path.join(root_abs_path, new_rel_path)

    if os.path.abspath(new_abs) == os.path.abspath(old_abs):
        return

    if not force and os.path.exists(new_abs):
        raise FileOperationError(
            f"Refusing to rename: destination already exists: {new_abs!r}"
        )

    collision = conn.execute(
        "SELECT asset_id FROM assets WHERE rel_path = ? AND asset_id != ?",
        (new_rel_path, asset_id),
    ).fetchone()
    if collision is not None:
        raise FileOperationError(
            f"Refusing to rename: another asset already occupies {new_rel_path!r}"
        )

    os.makedirs(os.path.dirname(new_abs) or ".", exist_ok=True)
    staging = new_abs + f"{STAGING_MARKER}{uuid.uuid4().hex}"
    try:
        os.rename(old_abs, staging)
    except OSError as e:
        raise FileOperationError(
            f"Failed to stage rename {old_abs!r}: {e}"
        ) from e

    try:
        with transaction(conn):
            conn.execute(
                "UPDATE assets SET rel_path = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE asset_id = ?",
                (new_rel_path, asset_id),
            )
        os.rename(staging, new_abs)
    except BaseException:
        # Best-effort: put the file back where it was.
        try:
            if os.path.exists(staging):
                os.rename(staging, old_abs)
        except OSError:
            pass
        raise


def delete_asset(
    conn: sqlite3.Connection, root_abs_path: str, asset_id: str
) -> None:
    """
    Delete an asset's file from disk and remove the DB row.

    Stage the file to a sibling temp name first; commit the DB delete; then
    unlink. If the unlink fails after a successful commit, the staged file
    is left in place and an error is raised so the operator can clean up.
    """
    asset_id = resolve_merged_id(conn, asset_id)
    asset = get_asset(conn, asset_id)
    abs_path = os.path.join(root_abs_path, asset.rel_path)

    staging: Optional[str] = None
    if os.path.exists(abs_path):
        staging = abs_path + f"{STAGING_MARKER}{uuid.uuid4().hex}"
        try:
            os.rename(abs_path, staging)
        except OSError as e:
            raise FileOperationError(
                f"Failed to stage delete {abs_path!r}: {e}"
            ) from e

    try:
        with transaction(conn):
            conn.execute("DELETE FROM assets WHERE asset_id = ?", (asset_id,))
    except BaseException:
        if staging is not None:
            try:
                os.rename(staging, abs_path)
            except OSError:
                pass
        raise

    if staging is not None:
        try:
            os.remove(staging)
        except OSError as e:
            raise FileOperationError(
                f"DB row deleted but failed to remove staged file {staging!r}: {e}"
            ) from e


# ---------------------------------------------------------------------------
# Merge (within a single shard)
# ---------------------------------------------------------------------------

def merge_assets(
    conn: sqlite3.Connection, survivor_id: str, merged_id: str
) -> None:
    """
    Merge merged_id into survivor_id (both within this shard).

    - tags: union
    - captions: per kind, keep longer content (survivor wins ties)
    - hash history: merged asset's current and prior hashes move to survivor
    - merged_assets row inserted; prior rows pointing at merged_id rewritten
    - merged asset row deleted

    Note: the file formerly associated with merged_id is NOT removed from
    disk. Merge is a metadata operation. Delete the file separately if
    desired.
    """
    survivor_id = resolve_merged_id(conn, survivor_id)
    merged_id = resolve_merged_id(conn, merged_id)

    if survivor_id == merged_id:
        raise MergeError(f"Cannot merge asset {survivor_id!r} into itself")

    get_asset(conn, survivor_id)
    merged = get_asset(conn, merged_id)

    with transaction(conn):
        conn.execute(
            """
            INSERT OR IGNORE INTO asset_tags(asset_id, tag_id)
            SELECT ?, tag_id FROM asset_tags WHERE asset_id = ?
            """,
            (survivor_id, merged_id),
        )

        merged_caps = conn.execute(
            "SELECT kind, content FROM captions WHERE asset_id = ?", (merged_id,)
        ).fetchall()
        for cap in merged_caps:
            kind, merged_content = cap["kind"], cap["content"]
            survivor_row = conn.execute(
                "SELECT content FROM captions WHERE asset_id = ? AND kind = ?",
                (survivor_id, kind),
            ).fetchone()
            if survivor_row is None:
                conn.execute(
                    "INSERT INTO captions(asset_id, kind, content) VALUES (?, ?, ?)",
                    (survivor_id, kind, merged_content),
                )
            elif len(merged_content) > len(survivor_row["content"]):
                conn.execute(
                    """
                    UPDATE captions SET content = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE asset_id = ? AND kind = ?
                    """,
                    (merged_content, survivor_id, kind),
                )

        conn.execute(
            "INSERT OR IGNORE INTO asset_hash_history(asset_id, hash) VALUES (?, ?)",
            (survivor_id, merged.file_hash),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO asset_hash_history(asset_id, hash, replaced_at)
            SELECT ?, hash, replaced_at FROM asset_hash_history WHERE asset_id = ?
            """,
            (survivor_id, merged_id),
        )

        conn.execute(
            "UPDATE merged_assets SET new_asset_id = ? WHERE new_asset_id = ?",
            (survivor_id, merged_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO merged_assets(old_asset_id, new_asset_id) "
            "VALUES (?, ?)",
            (merged_id, survivor_id),
        )

        conn.execute("DELETE FROM assets WHERE asset_id = ?", (merged_id,))


# ---------------------------------------------------------------------------
# Caption search (per shard)
# ---------------------------------------------------------------------------

def search_captions(
    conn: sqlite3.Connection, match_expr: str
) -> list[tuple[str, str, str]]:
    """
    FTS5 MATCH query against this shard's captions. Returns a list of
    (asset_id, kind, content).
    """
    rows = conn.execute(
        """
        SELECT c.asset_id, c.kind, c.content
        FROM captions c JOIN captions_fts f ON f.rowid = c.caption_id
        WHERE captions_fts MATCH ?
        """,
        (match_expr,),
    ).fetchall()
    return [(r["asset_id"], r["kind"], r["content"]) for r in rows]


# ---------------------------------------------------------------------------
# Datasets (per shard)
# ---------------------------------------------------------------------------

def add_to_dataset(
    conn: sqlite3.Connection,
    name: str,
    asset_ids: Iterable[str],
    description: str = "",
) -> None:
    """
    Add assets to a named dataset in this shard. Creates the dataset row
    lazily on first use. Assets must belong to this shard (enforced by FK).
    """
    with transaction(conn):
        conn.execute(
            "INSERT OR IGNORE INTO datasets(name, description) VALUES (?, ?)",
            (name, description),
        )
        for asset_id in asset_ids:
            conn.execute(
                "INSERT OR IGNORE INTO dataset_assets(dataset_name, asset_id) VALUES (?, ?)",
                (name, asset_id),
            )


def remove_from_dataset(
    conn: sqlite3.Connection,
    name: str,
    asset_ids: Iterable[str],
) -> None:
    """
    Remove assets from a dataset. If no members remain in this shard, the
    dataset row is removed too — datasets exist only where they have assets.
    """
    with transaction(conn):
        for asset_id in asset_ids:
            conn.execute(
                "DELETE FROM dataset_assets WHERE dataset_name = ? AND asset_id = ?",
                (name, asset_id),
            )
        remaining = conn.execute(
            "SELECT COUNT(*) FROM dataset_assets WHERE dataset_name = ?", (name,)
        ).fetchone()[0]
        if remaining == 0:
            conn.execute("DELETE FROM datasets WHERE name = ?", (name,))


def rename_dataset(conn: sqlite3.Connection, old_name: str, new_name: str) -> None:
    """Rename a dataset within this shard (both the header row and all memberships)."""
    with transaction(conn):
        # defer_foreign_keys delays FK checks to commit time so both tables can
        # be updated within a single transaction without a transient violation.
        conn.execute("PRAGMA defer_foreign_keys = ON")
        conn.execute("UPDATE datasets SET name = ? WHERE name = ?", (new_name, old_name))
        conn.execute("UPDATE dataset_assets SET dataset_name = ? WHERE dataset_name = ?", (new_name, old_name))


def delete_dataset(conn: sqlite3.Connection, name: str) -> None:
    """Remove a dataset and all its memberships from this shard."""
    with transaction(conn):
        conn.execute("DELETE FROM datasets WHERE name = ?", (name,))


def list_datasets(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """Return (name, description, member_count) for all datasets in this shard."""
    rows = conn.execute(
        """
        SELECT d.name, d.description, COUNT(da.asset_id) AS count
        FROM datasets d
        LEFT JOIN dataset_assets da ON da.dataset_name = d.name
        GROUP BY d.name
        ORDER BY d.name
        """
    ).fetchall()
    return [(r["name"], r["description"], r["count"]) for r in rows]
