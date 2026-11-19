"""SQLite access helpers.

Schema (user_version = 1)
-------------------------
A *payload* is the pair (firmware version, raw word).  When the valve opening
is stable many devices report the same measurement word, so the payload and its
decoded result are stored exactly once and shared by every frame referencing
it.  Frames keep their own per-frame reception/decoding timestamps.

A database written by the previous schema version (one wide ``frames`` table)
is upgraded in-place inside a single transaction: every original frame field
and value is preserved, and any failure rolls the whole migration back so the
old database stays intact and the next startup retries.
"""

import os
import sqlite3
import time

from flask import current_app, g

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS firmware (
        fw_version    TEXT PRIMARY KEY,
        word_order    TEXT NOT NULL CHECK (word_order IN ('high_first', 'low_first')),
        registered_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS payloads (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        fw_version  TEXT NOT NULL,
        raw_word    TEXT NOT NULL,
        status      TEXT NOT NULL CHECK (status IN ('decoded', 'invalid', 'pending')),
        reading     REAL,
        word_order  TEXT,
        bit_pattern TEXT,
        decoded_at  TEXT,
        UNIQUE (fw_version, raw_word)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS frames (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id   TEXT NOT NULL,
        frame_no    INTEGER NOT NULL,
        payload_id  INTEGER NOT NULL REFERENCES payloads (id),
        received_at TEXT NOT NULL,
        decoded_at  TEXT,
        UNIQUE (device_id, frame_no)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_payloads_fw_status ON payloads (fw_version, status)",
    "CREATE INDEX IF NOT EXISTS idx_frames_payload ON frames (payload_id)",
    "CREATE INDEX IF NOT EXISTS idx_frames_device ON frames (device_id, frame_no)",
]

USER_VERSION = 1


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            timeout=30,
            isolation_level=None,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 30000")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


class read_snapshot:
    """Run several SELECTs against one consistent snapshot.

    This keeps a page from mixing data from before and after a concurrent
    transaction (for example firmware registration + backfill).
    """

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("ROLLBACK" if exc_type is not None else "COMMIT")
        return False


def _table_columns(db, table):
    # The bootstrap connection may not use sqlite3.Row, so index by position.
    return {r[1] for r in db.execute("PRAGMA table_info(%s)" % table).fetchall()}


def _migrate_legacy(db):
    """Upgrade the original wide ``frames`` table to payloads + frames.

    Runs entirely inside the caller's write transaction.  Frame ids and every
    original value are carried over verbatim; decoding results move to the
    payload layer and are shared by frames with the same (fw_version, raw_word).
    """
    db.execute(
        """
        CREATE TABLE payloads (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            fw_version  TEXT NOT NULL,
            raw_word    TEXT NOT NULL,
            status      TEXT NOT NULL CHECK (status IN ('decoded', 'invalid', 'pending')),
            reading     REAL,
            word_order  TEXT,
            bit_pattern TEXT,
            decoded_at  TEXT,
            UNIQUE (fw_version, raw_word)
        )
        """
    )
    # One row per distinct payload; MIN keeps the earliest decoding time, which
    # is the moment that content first became a decoded result.
    db.execute(
        """
        INSERT INTO payloads
            (id, fw_version, raw_word, status, reading,
             word_order, bit_pattern, decoded_at)
        SELECT MIN(id), fw_version, raw_word, status, reading,
               word_order, bit_pattern, MIN(decoded_at)
        FROM frames
        GROUP BY fw_version, raw_word
        """
    )
    db.execute(
        """
        CREATE TABLE frames_new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id   TEXT NOT NULL,
            frame_no    INTEGER NOT NULL,
            payload_id  INTEGER NOT NULL REFERENCES payloads (id),
            received_at TEXT NOT NULL,
            decoded_at  TEXT,
            UNIQUE (device_id, frame_no)
        )
        """
    )
    db.execute(
        """
        INSERT INTO frames_new
            (id, device_id, frame_no, payload_id, received_at, decoded_at)
        SELECT f.id, f.device_id, f.frame_no, p.id, f.received_at, f.decoded_at
        FROM frames f
        JOIN payloads p ON p.fw_version = f.fw_version
                       AND p.raw_word = f.raw_word
        """
    )

    old_count = db.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    new_count = db.execute("SELECT COUNT(*) FROM frames_new").fetchone()[0]
    orphan_count = db.execute(
        """
        SELECT COUNT(*) FROM frames f
        WHERE NOT EXISTS (
            SELECT 1 FROM payloads p
            WHERE p.fw_version = f.fw_version AND p.raw_word = f.raw_word
        )
        """
    ).fetchone()[0]
    if new_count != old_count or orphan_count != 0:
        # Roll the whole migration back; the original database stays usable.
        raise sqlite3.DatabaseError(
            "载荷表迁移校验失败：帧 %d -> %d，孤立帧 %d"
            % (old_count, new_count, orphan_count)
        )

    db.execute("DROP TABLE frames")
    db.execute("ALTER TABLE frames_new RENAME TO frames")
    db.execute(
        "CREATE INDEX idx_payloads_fw_status ON payloads (fw_version, status)"
    )
    db.execute("CREATE INDEX idx_frames_payload ON frames (payload_id)")
    db.execute("CREATE INDEX idx_frames_device ON frames (device_id, frame_no)")
    db.execute("PRAGMA user_version = %d" % USER_VERSION)


def _init_once(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version >= USER_VERSION:
            db.execute("COMMIT")
            return

        tables = {
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "frames" not in tables:
            # Brand new database.
            for statement in SCHEMA_STATEMENTS:
                db.execute(statement)
            db.execute("PRAGMA user_version = %d" % USER_VERSION)
        elif "raw_word" in _table_columns(db, "frames"):
            # Legacy volume: upgrade without losing any frame field or value.
            _migrate_legacy(db)
        else:
            raise sqlite3.DatabaseError(
                "无法识别的数据库结构（user_version=%d）" % version
            )
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def init_db():
    db = sqlite3.connect(current_app.config["DATABASE"], timeout=30)
    try:
        db.execute("PRAGMA busy_timeout = 30000")
        deadline = time.monotonic() + 30
        while True:
            try:
                db.execute("PRAGMA journal_mode = WAL")
                _init_once(db)
                break
            except sqlite3.OperationalError as exc:
                # Retry only on writer contention; a real schema error must
                # surface immediately instead of spinning until the deadline.
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    raise
                # Several workers may boot at once; the write lock serialises
                # schema creation and migration. Retry until one wins.
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
    finally:
        db.close()


def init_app(app):
    app.teardown_appcontext(close_db)
    os.makedirs(app.config["DATA_DIR"], exist_ok=True)
    with app.app_context():
        init_db()
