"""SQLite access helpers."""

import os
import sqlite3
import time
from contextlib import contextmanager

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS firmware (
    fw_version   TEXT PRIMARY KEY,
    word_order   TEXT NOT NULL CHECK (word_order IN ('high_first', 'low_first')),
    registered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS frames (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id     TEXT NOT NULL,
    frame_no      INTEGER NOT NULL,
    fw_version    TEXT NOT NULL,
    raw_word      TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('decoded', 'invalid', 'pending')),
    reading       REAL,
    word_order    TEXT,
    bit_pattern   TEXT,
    received_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    decoded_at    TEXT,
    UNIQUE (device_id, frame_no)
);

CREATE INDEX IF NOT EXISTS idx_frames_fw_status ON frames (fw_version, status);
CREATE INDEX IF NOT EXISTS idx_frames_device ON frames (device_id, frame_no);
"""


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


@contextmanager
def read_snapshot(conn):
    """Run several SELECTs against one consistent snapshot.

    This keeps a page from mixing data from before and after a concurrent
    transaction (for example firmware registration + backfill).
    """
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db():
    db = sqlite3.connect(current_app.config["DATABASE"], timeout=30)
    try:
        db.execute("PRAGMA busy_timeout = 30000")
        deadline = time.monotonic() + 30
        while True:
            try:
                db.execute("PRAGMA journal_mode = WAL")
                db.executescript(SCHEMA)
                db.commit()
                break
            except sqlite3.OperationalError:
                # Several workers may boot at once; retry until one wins.
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
