"""Frame parsing, IEEE-754 decoding and batch ingestion rules.

The numeric input is a single 32-bit value rendered as 8 hex digits.  The two
16-bit words may be transmitted either high-word-first or low-word-first,
depending on firmware.  Only an explicitly registered word order is ever used;
unknown firmware is stored as "pending" and never guessed.
"""

import csv
import io
import math
import re
import struct

WORD_ORDERS = {
    "high_first": "高字在前",
    "low_first": "低字在前",
}

HEX8_RE = re.compile(r"^[0-9A-Fa-f]{8}$")
FRAME_NO_RE = re.compile(r"^[0-9]+$")
MAX_FRAME_NO = 2**53  # keep frame numbers exact when rendered as JSON numbers
MAX_BATCH = 200


class ValidationError(ValueError):
    """A row or the whole upload is malformed."""


def assemble(raw_word, word_order):
    """Return the 32-bit pattern after applying the firmware word order."""
    high_word = int(raw_word[0:4], 16)
    low_word = int(raw_word[4:8], 16)
    if word_order == "high_first":
        return (high_word << 16) | low_word
    if word_order == "low_first":
        return (low_word << 16) | high_word
    raise ValueError("unknown word order: %r" % (word_order,))


def decode(raw_word, word_order):
    """Decode to a float; return (status, reading, bit_pattern).

    Non-finite IEEE-754 single values (NaN, +/-Inf) are marked invalid.
    """
    pattern = assemble(raw_word, word_order)
    (value,) = struct.unpack(">f", struct.pack(">I", pattern))
    bits = "%08X" % pattern
    if not math.isfinite(value):
        return "invalid", None, bits
    return "decoded", value, bits


def content_key(raw_word, fw_version):
    """Identity of a frame's content for retransmission comparisons.

    Device and frame number identify the slot; raw word and firmware version
    are the payload.  A frame slot is immutable once stored, so any difference
    means the upload contradicts existing data.
    """
    return (raw_word.upper(), fw_version)


def parse_row(fields, line_no):
    """Parse one CSV row (already split) into a dict. Raises ValidationError."""
    parts = [p.strip() for p in fields]
    if len(parts) != 4:
        raise ValidationError(
            "第 %d 行：应为 4 列（设备号,帧号,固件号,原始字），实际 %d 列"
            % (line_no, len(parts))
        )
    device_id, frame_no, fw_version, raw_word = parts

    if not device_id:
        raise ValidationError("第 %d 行：设备号不能为空" % line_no)
    if len(device_id) > 128:
        raise ValidationError("第 %d 行：设备号过长" % line_no)
    if not fw_version:
        raise ValidationError("第 %d 行：固件号不能为空" % line_no)
    if len(fw_version) > 128:
        raise ValidationError("第 %d 行：固件号过长" % line_no)
    if not FRAME_NO_RE.match(frame_no):
        raise ValidationError("第 %d 行：帧号必须是非负整数" % line_no)
    frame_no_int = int(frame_no)
    if frame_no_int >= MAX_FRAME_NO:
        raise ValidationError("第 %d 行：帧号超出范围" % line_no)
    if not HEX8_RE.match(raw_word):
        raise ValidationError(
            "第 %d 行：原始字必须是 8 位十六进制（如 3F800000）" % line_no
        )

    return {
        "device_id": device_id,
        "frame_no": frame_no_int,
        "frame_no_text": frame_no,
        "fw_version": fw_version,
        "raw_word": raw_word.upper(),
        "content": content_key(raw_word, fw_version),
    }


HEADER_FIELDS = {
    "设备号": "device_id",
    "device": "device_id",
    "device_id": "device_id",
    "帧号": "frame_no",
    "frame": "frame_no",
    "frame_no": "frame_no",
    "固件号": "fw_version",
    "firmware": "fw_version",
    "fw": "fw_version",
    "fw_version": "fw_version",
    "原始字": "raw_word",
    "raw": "raw_word",
    "raw_word": "raw_word",
}


def _looks_like_header(fields):
    found = {p.strip().lower() for p in fields}
    if len(found) < 3:
        return False
    return len(found & set(HEADER_FIELDS)) >= 3


def parse_csv(text):
    """Parse a whole upload into rows, validating the entire batch first.

    Uses the csv module so quoted fields and mixed line endings work; a header
    row (Chinese or English names) is ignored if present.
    """
    if text.startswith("﻿"):
        text = text[1:]
    rows = []
    reader = csv.reader(io.StringIO(text, newline=""))
    for idx, fields in enumerate(reader, start=1):
        if not any(f.strip() for f in fields):
            continue
        if not rows and _looks_like_header(fields):
            continue
        rows.append(parse_row(fields, idx))

    if not rows:
        raise ValidationError("上传内容为空")
    if len(rows) > MAX_BATCH:
        raise ValidationError("每批至多 %d 帧，本次 %d 帧" % (MAX_BATCH, len(rows)))

    # A batch may not contradict itself: same device+frame with different
    # content is rejected as a whole.
    seen = {}
    for row in rows:
        key = (row["device_id"], row["frame_no"])
        prev = seen.get(key)
        if prev is not None and prev != row["content"]:
            raise ValidationError(
                "同一批中设备 %s 帧号 %d 出现不同内容（%s 与 %s）"
                % (row["device_id"], row["frame_no"], prev, row["content"])
            )
        seen[key] = row["content"]
    return rows
