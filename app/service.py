"""Transactional business logic for uploads and firmware registration.

Storage is split into two layers:

* ``payloads`` -- the distinct (firmware version, raw word) contents with their
  shared decoding result.  Each distinct payload is decoded at most once.
* ``frames`` -- one row per received frame, referencing its payload and keeping
  its own reception/decoding timestamps.
"""

from contextlib import contextmanager
from datetime import datetime, timezone

from . import decoder


class ConflictError(Exception):
    """Request conflicts with stored, immutable state."""


class BatchRejected(Exception):
    """The whole upload is rejected (e.g. content mismatch on retransmit)."""

    def __init__(self, message, conflicts=None):
        super().__init__(message)
        self.conflicts = conflicts or []


def utc_now():
    """UTC timestamp with millisecond precision, matching SQLite '%f' style."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (
        now.microsecond // 1000
    )


@contextmanager
def write_transaction(db):
    """Single serialised writer; everything commits or nothing does."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


def _firmware_orders(db, versions):
    versions = list(versions)
    if not versions:
        return {}
    placeholders = ",".join("?" for _ in versions)
    rows = db.execute(
        "SELECT fw_version, word_order FROM firmware WHERE fw_version IN (%s)"
        % placeholders,
        versions,
    ).fetchall()
    return {r["fw_version"]: r["word_order"] for r in rows}


def _get_payload(db, fw_version, raw_word):
    return db.execute(
        "SELECT * FROM payloads WHERE fw_version = ? AND raw_word = ?",
        (fw_version, raw_word),
    ).fetchone()


def ingest(db, rows):
    """Apply a validated batch atomically.

    Rules:
      * distinct (fw_version, raw_word) payloads are stored once and share one
        decoding result;
      * unknown firmware -> payload stored pending, no guessed reading;
      * registered firmware -> each new payload is decoded exactly once;
      * a new frame referencing an already decoded payload reuses the result
        but gets this reception's time as its frame-level decoded_at;
      * retransmission (same device+frame+content) -> reuse the stored frame,
        its timestamps never change;
      * same device+frame with different content -> reject the whole batch.
    """
    summary = {
        "received": len(rows),
        "inserted": 0,
        "reused": 0,
        "decoded": 0,
        "invalid": 0,
        "pending": 0,
        "new_payloads": 0,
        "inserted_keys": [],
    }

    with write_transaction(db):
        orders = _firmware_orders(db, {r["fw_version"] for r in rows})
        now = utc_now()

        # Reject the whole batch before touching anything.  The frame slot's
        # content (firmware + raw word) is immutable.
        existing = {}
        conflicts = []
        for row in rows:
            key = (row["device_id"], row["frame_no"])
            if key in existing:
                continue
            prev = db.execute(
                """
                SELECT p.raw_word, p.fw_version
                FROM frames f JOIN payloads p ON p.id = f.payload_id
                WHERE f.device_id = ? AND f.frame_no = ?
                """,
                key,
            ).fetchone()
            existing[key] = prev
            if prev is not None and (
                prev["raw_word"] != row["raw_word"]
                or prev["fw_version"] != row["fw_version"]
            ):
                conflicts.append(
                    {
                        "device_id": row["device_id"],
                        "frame_no": row["frame_no"],
                        "stored_raw_word": "%s (%s)"
                        % (prev["raw_word"], prev["fw_version"]),
                        "uploaded_raw_word": "%s (%s)"
                        % (row["raw_word"], row["fw_version"]),
                    }
                )
        if conflicts:
            raise BatchRejected(
                "上传被拒绝：%d 帧与该设备已存帧号内容不一致" % len(conflicts),
                conflicts,
            )

        inserted_in_batch = set()
        for row in rows:
            key = (row["device_id"], row["frame_no"])
            if existing.get(key) is not None or key in inserted_in_batch:
                # Same-number same-content retransmission: keep the original
                # frame and its original timestamps.
                summary["reused"] += 1
                continue

            fw = row["fw_version"]
            payload = _get_payload(db, fw, row["raw_word"])
            if payload is None:
                # Each distinct pending payload is decoded at most once, here.
                order = orders.get(fw)
                if order is None:
                    status, reading, bits = "pending", None, None
                    summary["pending"] += 1
                else:
                    status, reading, bits = decoder.decode(row["raw_word"], order)
                    if status == "decoded":
                        summary["decoded"] += 1
                    else:
                        summary["invalid"] += 1
                cur = db.execute(
                    """
                    INSERT INTO payloads
                        (fw_version, raw_word, status, reading,
                         word_order, bit_pattern, decoded_at)
                    VALUES (?, ?, ?, ?, ?, ?,
                            CASE WHEN ? IS NULL THEN NULL ELSE ? END)
                    """,
                    (
                        fw,
                        row["raw_word"],
                        status,
                        reading,
                        order,
                        bits,
                        bits,
                        now,
                    ),
                )
                payload_id = cur.lastrowid
                summary["new_payloads"] += 1
                # A frame arriving together with a freshly decoded payload is
                # decoded at this reception.
                frame_decoded_at = now if bits is not None else None
            else:
                payload_id = payload["id"]
                # Reuse the shared decoding result; the new frame still times
                # its own decoding at this reception.
                frame_decoded_at = (
                    now if payload["status"] != "pending" else None
                )

            db.execute(
                """
                INSERT INTO frames
                    (device_id, frame_no, payload_id, received_at, decoded_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    row["device_id"],
                    row["frame_no"],
                    payload_id,
                    now,
                    frame_decoded_at,
                ),
            )
            inserted_in_batch.add(key)
            summary["inserted"] += 1
            summary["inserted_keys"].append(key)

    return summary


def register_firmware(db, fw_version, word_order):
    """Register an immutable word order and backfill its pending payloads.

    Registration and backfill happen in one transaction, so no reader can
    observe the firmware as configured while its payloads are still pending.
    Backfill writes only to the payload layer -- once per distinct pending
    payload -- and every frame referencing those payloads then shows the shared
    result with this backfill time.
    """
    if word_order not in decoder.WORD_ORDERS:
        raise ConflictError("字序只能是 high_first 或 low_first")

    with write_transaction(db):
        row = db.execute(
            "SELECT word_order FROM firmware WHERE fw_version = ?", (fw_version,)
        ).fetchone()
        already = row is not None
        if already:
            if row["word_order"] != word_order:
                raise ConflictError(
                    "固件 %s 已登记为「%s」，字序不可更改（请求「%s」）"
                    % (
                        fw_version,
                        decoder.WORD_ORDERS[row["word_order"]],
                        decoder.WORD_ORDERS[word_order],
                    )
                )
        else:
            db.execute(
                "INSERT INTO firmware (fw_version, word_order, registered_at) "
                "VALUES (?, ?, ?)",
                (fw_version, word_order, utc_now()),
            )

        # One row per distinct pending payload: each is decoded exactly once.
        pending_payloads = db.execute(
            """
            SELECT id, raw_word FROM payloads
            WHERE fw_version = ? AND status = 'pending'
            """,
            (fw_version,),
        ).fetchall()

        now = utc_now()
        decoded = invalid = frames_backfilled = 0
        for payload in pending_payloads:
            status, reading, bits = decoder.decode(payload["raw_word"], word_order)
            # The pending guard makes repeated registration safe: a payload
            # can never be decoded twice.
            count = db.execute(
                """
                UPDATE payloads
                SET status = ?, reading = ?, word_order = ?, bit_pattern = ?,
                    decoded_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (status, reading, word_order, bits, now, payload["id"]),
            ).rowcount
            if count == 1:
                frames_backfilled += db.execute(
                    "UPDATE frames SET decoded_at = ? WHERE payload_id = ?",
                    (now, payload["id"]),
                ).rowcount
                if status == "decoded":
                    decoded += 1
                else:
                    invalid += 1

    return {
        "fw_version": fw_version,
        "word_order": word_order,
        "already_registered": already,
        "backfilled": decoded + invalid,
        "frames_backfilled": frames_backfilled,
        "decoded": decoded,
        "invalid": invalid,
    }


def totals(db):
    row = db.execute(
        "SELECT COUNT(*) AS frame_count, "
        "(SELECT COUNT(*) FROM payloads) AS payload_count "
        "FROM frames"
    ).fetchone()
    return dict(row)


def list_devices(db):
    rows = db.execute(
        """
        SELECT f.device_id,
               COUNT(*) AS frame_count,
               COUNT(DISTINCT f.payload_id) AS payload_count,
               SUM(CASE WHEN p.status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
               MAX(f.received_at) AS last_seen
        FROM frames f JOIN payloads p ON p.id = f.payload_id
        GROUP BY f.device_id
        ORDER BY f.device_id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def list_firmwares(db):
    rows = db.execute(
        """
        SELECT fw.fw_version, fw.word_order, fw.registered_at,
               (SELECT COUNT(*) FROM payloads p WHERE p.fw_version = fw.fw_version)
                   AS payload_count,
               (SELECT COUNT(*) FROM payloads p
                WHERE p.fw_version = fw.fw_version AND p.status = 'pending')
                   AS pending_count,
               (SELECT COUNT(*) FROM frames fr JOIN payloads p ON p.id = fr.payload_id
                WHERE p.fw_version = fw.fw_version)
                   AS frame_count
        FROM firmware fw
        GROUP BY fw.fw_version
        ORDER BY fw.fw_version
        """
    ).fetchall()
    return [dict(r) for r in rows]


_FRAME_SELECT = """
    SELECT f.device_id, f.frame_no, p.fw_version, p.raw_word, p.status,
           p.reading, p.word_order, p.bit_pattern,
           f.received_at, f.decoded_at
    FROM frames f JOIN payloads p ON p.id = f.payload_id
"""


def device_frames(db, device_id):
    """Per-frame raw word, status, reading and timestamps (device page / API)."""
    rows = db.execute(
        _FRAME_SELECT
        + " WHERE f.device_id = ? ORDER BY f.frame_no",
        (device_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def batch_frames(db, rows):
    """Current stored state of the frame slots in a submitted batch.

    Reading this inside one snapshot after the write commits means the upload
    response reflects a firmware registration that ran concurrently and had
    already committed, never a stale "pending".
    """
    result = []
    seen = set()
    for row in rows:
        key = (row["device_id"], row["frame_no"])
        if key in seen:
            continue
        seen.add(key)
        stored = db.execute(
            _FRAME_SELECT
            + " WHERE f.device_id = ? AND f.frame_no = ?",
            key,
        ).fetchone()
        if stored is not None:
            result.append(dict(stored))
    return result
