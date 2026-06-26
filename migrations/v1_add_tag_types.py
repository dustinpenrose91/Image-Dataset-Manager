"""
Schema migrations for per-shard SQLite databases.

Call apply_migrations(conn) from imgdb.init_shard before executescript(SCHEMA_SQL).

Deletion checklist: when all deployed databases have been upgraded past a migration,
delete its _maybe_* function, remove the call from apply_migrations, and when
apply_migrations is empty, delete this module and its call in imgdb.init_shard.
"""
import sqlite3


def apply_migrations(conn: sqlite3.Connection) -> None:
    _maybe_v1_add_tag_types(conn)


def _maybe_v1_add_tag_types(conn: sqlite3.Connection) -> None:
    # Runs only when upgrading a database that predates the tag_types table.
    # New databases get tag_types directly from SCHEMA_SQL, so they are skipped.
    has_tags = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tags'"
    ).fetchone()
    has_tag_types = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tag_types'"
    ).fetchone()
    if not (has_tags and not has_tag_types):
        return

    # PRAGMA foreign_keys cannot be changed inside an active transaction, and we
    # need it OFF to DROP and recreate a table that asset_tags references.
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("""
            CREATE TABLE tag_types (
                type_id    INTEGER PRIMARY KEY,
                name       TEXT    NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("INSERT INTO tag_types(type_id, name) VALUES (0, 'General')")
        # Recreate tags with (name, type_id) uniqueness instead of name alone.
        # All existing tags migrate to General (type_id = 0).
        conn.execute("""
            CREATE TABLE tags_new (
                tag_id  TEXT    PRIMARY KEY,
                name    TEXT    NOT NULL COLLATE NOCASE,
                type_id INTEGER NOT NULL DEFAULT 0
                        REFERENCES tag_types(type_id) ON DELETE RESTRICT,
                UNIQUE(name, type_id)
            )
        """)
        conn.execute(
            "INSERT INTO tags_new(tag_id, name, type_id) SELECT tag_id, name, 0 FROM tags"
        )
        conn.execute("DROP TABLE tags")
        conn.execute("ALTER TABLE tags_new RENAME TO tags")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
