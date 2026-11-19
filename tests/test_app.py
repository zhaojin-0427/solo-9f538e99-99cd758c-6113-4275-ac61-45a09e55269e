import io
import os
import sqlite3
import struct
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db, decoder, service  # noqa: E402


def hex_word(value, word_order="high_first"):
    """Encode a float as an 8-hex-digit raw field in the given wire order."""
    pattern = struct.unpack(">I", struct.pack(">f", value))[0]
    high = "%04X" % (pattern >> 16)
    low = "%04X" % (pattern & 0xFFFF)
    return high + low if word_order == "high_first" else low + high


class DecoderTests(unittest.TestCase):
    def test_high_first(self):
        self.assertEqual(decoder.decode("3F800000", "high_first")[1], 1.0)
        self.assertEqual(decoder.decode("40000000", "high_first")[1], 2.0)

    def test_low_first(self):
        self.assertEqual(decoder.decode("00003F80", "low_first")[1], 1.0)
        self.assertEqual(decoder.decode("00004000", "low_first")[1], 2.0)

    def test_non_finite_invalid(self):
        for raw in ("7FC00000", "7F800000", "FF800000", "7FFFFFFF"):
            status, reading, bits = decoder.decode(raw, "high_first")
            self.assertEqual(status, "invalid")
            self.assertIsNone(reading)
        status, _, _ = decoder.decode("00007FC0", "low_first")
        self.assertEqual(status, "invalid")

    def test_negative_zero_and_denormal_are_finite(self):
        status, reading, _ = decoder.decode("80000000", "high_first")
        self.assertEqual(status, "decoded")
        self.assertEqual(reading, 0.0)
        status, reading, _ = decoder.decode("00000001", "high_first")
        self.assertEqual(status, "decoded")
        self.assertGreater(reading, 0)

    def test_round_trip_random(self):
        for value in (0.0, -1.5, 3.14159, 1e-30, 1e-38, 1e38, -123456.0):
            for order in ("high_first", "low_first"):
                status, reading, _ = decoder.decode(hex_word(value, order), order)
                self.assertEqual(status, "decoded")
                expected = struct.unpack(">f", struct.pack(">f", value))[0]
                self.assertEqual(reading, expected)


class BatchRulesTests(unittest.TestCase):
    def test_rejects_too_many(self):
        text = "\n".join("D1,%d,FW1,3F800000" % i for i in range(201))
        with self.assertRaises(decoder.ValidationError):
            decoder.parse_csv(text)
        text = "\n".join("D1,%d,FW1,3F800000" % i for i in range(200))
        self.assertEqual(len(decoder.parse_csv(text)), 200)

    def test_rejects_bad_hex(self):
        with self.assertRaises(decoder.ValidationError):
            decoder.parse_csv("D1,1,FW1,3F80000")
        with self.assertRaises(decoder.ValidationError):
            decoder.parse_csv("D1,1,FW1,3F80000G")
        with self.assertRaises(decoder.ValidationError):
            decoder.parse_csv("D1,-1,FW1,3F800000")
        with self.assertRaises(decoder.ValidationError):
            decoder.parse_csv("D1,1,FW1,3F800000,extra")

    def test_intra_batch_conflict(self):
        with self.assertRaises(decoder.ValidationError):
            decoder.parse_csv("D1,1,FW1,3F800000\nD1,1,FW1,40000000")

    def test_duplicate_same_content_allowed(self):
        rows = decoder.parse_csv("D1,1,FW1,3f800000\nD1,1,FW1,3F800000")
        self.assertEqual(len(rows), 2)


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(
            {
                "TESTING": True,
                "DATA_DIR": self.tmp.name,
                "DATABASE": os.path.join(self.tmp.name, "test.sqlite3"),
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, text):
        data = {"csv": (io.BytesIO(text.encode("utf-8")), "frames.csv")}
        return self.client.post(
            "/upload", data=data, content_type="multipart/form-data"
        )

    def register(self, fw, order):
        return self.client.post(
            "/firmware", data={"fw_version": fw, "word_order": order}
        )

    def frames(self, device):
        with self.app.app_context():
            return service.device_frames(db.get_db(), device)

    def test_pending_then_backfill(self):
        self.upload("D1,1,FW1,00003F80")
        self.assertEqual(self.frames("D1")[0]["status"], "pending")
        self.assertIsNone(self.frames("D1")[0]["reading"])

        self.register("FW1", "low_first")
        frame = self.frames("D1")[0]
        self.assertEqual(frame["status"], "decoded")
        self.assertEqual(frame["reading"], 1.0)
        self.assertEqual(frame["word_order"], "low_first")

    def test_register_first_then_upload(self):
        self.register("FW1", "high_first")
        self.upload("D1,1,FW1,40490FDB")
        frame = self.frames("D1")[0]
        self.assertEqual(frame["status"], "decoded")
        self.assertAlmostEqual(frame["reading"], 3.1415927, places=6)

    def test_word_order_is_immutable(self):
        self.register("FW1", "high_first")
        self.register("FW1", "low_first")
        with self.app.app_context():
            fw = service.list_firmwares(db.get_db())
        self.assertEqual(fw[0]["word_order"], "high_first")

    def test_retransmission_reuses_result(self):
        self.register("FW1", "high_first")
        self.upload("D1,1,FW1,3F800000")
        self.upload("D1,1,FW1,3F800000")
        with self.app.app_context():
            conn = db.get_db()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0], 1)
            row = conn.execute("SELECT received_at, decoded_at FROM frames").fetchone()
            self.assertEqual(row["received_at"], row["decoded_at"])

    def test_conflicting_content_rejects_whole_batch(self):
        self.register("FW1", "high_first")
        self.upload("D1,1,FW1,3F800000")
        resp = self.upload("D1,2,FW1,3F800000\nD1,1,FW1,40000000")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("上传被拒绝", resp.get_data(as_text=True))
        with self.app.app_context():
            conn = db.get_db()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0], 1)
        self.assertEqual(self.frames("D1")[0]["raw_word"], "3F800000")

    def test_conflicting_firmware_is_different_content(self):
        self.register("FW1", "high_first")
        self.upload("D1,1,FW1,3F800000")
        self.upload("D1,1,FW2,3F800000")
        frame = self.frames("D1")[0]
        self.assertEqual(frame["fw_version"], "FW1")

    def test_registration_backfills_all_pending_only(self):
        self.upload("D1,1,FW1,3F800000\nD1,2,FW1,7FC00000\nD1,3,FW2,3F800000")
        self.register("FW1", "high_first")
        frames = {f["frame_no"]: f for f in self.frames("D1")}
        self.assertEqual(frames[1]["status"], "decoded")
        self.assertEqual(frames[2]["status"], "invalid")
        self.assertIsNone(frames[2]["reading"])
        self.assertEqual(frames[3]["status"], "pending")

    def test_idempotent_registration_does_not_redecode(self):
        self.upload("D1,1,FW1,3F800000")
        self.register("FW1", "high_first")
        with self.app.app_context():
            conn = db.get_db()
            before = conn.execute("SELECT decoded_at FROM frames").fetchone()[0]
        self.register("FW1", "high_first")
        with self.app.app_context():
            conn = db.get_db()
            after = conn.execute("SELECT decoded_at FROM frames").fetchone()[0]
        self.assertEqual(before, after)

    def test_no_pending_visible_after_registration(self):
        self.upload("D1,1,FW1,3F800000")
        self.register("FW1", "high_first")
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("待解</td>", html)

    def test_unknown_firmware_never_decoded(self):
        self.upload("D1,1,UNKNOWN,3F800000")
        frame = self.frames("D1")[0]
        self.assertEqual(frame["status"], "pending")
        self.assertIsNone(frame["reading"])
        self.assertIsNone(frame["bit_pattern"])

    def test_device_view_survives_restart(self):
        self.register("FW1", "high_first")
        self.upload("D1,1,FW1,3F800000")
        # Simulate a restart with a fresh app object on the same database.
        app2 = create_app(
            {
                "TESTING": True,
                "DATA_DIR": self.tmp.name,
                "DATABASE": os.path.join(self.tmp.name, "test.sqlite3"),
            }
        )
        client2 = app2.test_client()
        html = client2.get("/devices/D1").get_data(as_text=True)
        self.assertIn("3F800000", html)
        self.assertIn("1", html)

    def test_device_404(self):
        self.assertEqual(self.client.get("/devices/NOPE").status_code, 404)

    def test_json_api(self):
        self.register("FW1", "high_first")
        self.upload("D1,1,FW1,3F800000")
        body = self.client.get("/api/devices/D1").get_json()
        self.assertEqual(body["frames"][0]["reading"], 1.0)

    def test_upload_without_file(self):
        resp = self.client.post("/upload", data={}, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 302)

    def test_blank_lines_and_uppercase(self):
        self.upload("\n D1 , 1 , FW1 , 3f800000 \n\n")
        frame = self.frames("D1")[0]
        self.assertEqual(frame["raw_word"], "3F800000")
        self.assertEqual(frame["status"], "pending")

    def test_api_upload_and_conflict(self):
        resp = self.client.post(
            "/api/upload", data="D1,1,FW1,3F800000\n", content_type="text/csv"
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["summary"]["pending"], 1)
        self.assertEqual(body["frames"][0]["status"], "pending")

        conflict = self.client.post(
            "/api/upload", data="D1,1,FW1,40000000\n", content_type="text/csv"
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertTrue(conflict.get_json()["rejected"])
        self.assertEqual(self.frames("D1")[0]["raw_word"], "3F800000")

    def test_api_firmware_immutable(self):
        self.client.post("/api/upload", data="D1,1,FW1,3F800000\n", content_type="text/csv")
        r1 = self.client.post(
            "/api/firmware", json={"fw_version": "FW1", "word_order": "high_first"}
        )
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r1.get_json()["backfilled"], 1)
        r2 = self.client.post(
            "/api/firmware", json={"fw_version": "FW1", "word_order": "low_first"}
        )
        self.assertEqual(r2.status_code, 409)
        self.assertEqual(self.frames("D1")[0]["reading"], 1.0)

    def test_api_unknown_device_is_404(self):
        self.assertEqual(self.client.get("/api/devices/NONE").status_code, 404)

    def test_api_devices_list(self):
        self.upload("D1,1,FW1,3F800000\nD2,1,FW1,3F800000")
        body = self.client.get("/api/devices").get_json()
        self.assertEqual([d["device_id"] for d in body["devices"]], ["D1", "D2"])

    def test_upload_json_form_field(self):
        resp = self.client.post(
            "/upload", data={"csv": "D1,1,FW1,3F800000"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.frames("D1")), 1)

    def test_invalid_batch_keeps_state(self):
        self.register("FW1", "high_first")
        self.upload("D1,1,FW1,3F800000")
        before = self.frames("D1")
        resp = self.upload("D1,2,FW1,ZZZZZZZZ")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.frames("D1"), before)

    def test_large_frame_numbers_round_trip(self):
        self.upload("D1,9007199254740991,FW1,3F800000")
        body = self.client.get("/api/devices/D1").get_json()
        self.assertEqual(body["frames"][0]["frame_no"], 9007199254740991)
        resp = self.upload("D1,9007199254740992,FW1,3F800000")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(self.frames("D1")), 1)


if __name__ == "__main__":
    unittest.main()


class ConcurrencyTests(unittest.TestCase):
    """Registration and upload racing must never lose or duplicate decodes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "race.sqlite3")
        self.app = create_app(
            {
                "TESTING": True,
                "DATA_DIR": self.tmp.name,
                "DATABASE": self.db_path,
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, fn, *args):
        with self.app.app_context():
            conn = db.get_db()
            return fn(conn, *args)

    def test_upload_and_registration_race(self):
        import threading

        rows = [
            {"device_id": "DEV", "frame_no": i, "fw_version": "FW1",
             "raw_word": hex_word(float(i), "low_first"), "content": None}
            for i in range(1, 60)
        ]

        # Half the frames arrive before registration, half after, racing.
        before, after = rows[:30], rows[30:]
        barrier = threading.Barrier(2)
        errors = []

        def uploader():
            try:
                barrier.wait()
                time.sleep(0.01)
                self._run(service.ingest, after)
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        def registrar():
            try:
                barrier.wait()
                self._run(service.register_firmware, "FW1", "low_first")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        self._run(service.ingest, before)
        threads = [threading.Thread(target=uploader), threading.Thread(target=registrar)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

        with self.app.app_context():
            conn = db.get_db()
            frames = conn.execute(
                """
                SELECT f.frame_no, p.status, p.reading, p.raw_word
                FROM frames f JOIN payloads p ON p.id = f.payload_id
                ORDER BY f.frame_no
                """
            ).fetchall()
        self.assertEqual(len(frames), 59)
        for f in frames:
            self.assertEqual(f["status"], "decoded")
            self.assertAlmostEqual(f["reading"], float(f["frame_no"]), places=5)

    def test_repeated_registration_never_duplicates_decode(self):
        self._run(service.ingest, [
            {"device_id": "DEV", "frame_no": i, "fw_version": "FW1",
             "raw_word": "3F800000", "content": None}
            for i in range(1, 10)
        ])
        import threading
        results = []

        def go():
            results.append(self._run(service.register_firmware, "FW1", "high_first"))
        threads = [threading.Thread(target=go) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Nine frames share one payload (same fw + raw word), so the payload is
        # backfilled exactly once across every racing registration.
        self.assertEqual(sum(r["backfilled"] for r in results), 1)
        self.assertEqual(sum(r["frames_backfilled"] for r in results), 9)
        with self.app.app_context():
            conn = db.get_db()
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM payloads WHERE status='pending'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM payloads").fetchone()[0], 1
            )


class HeaderTests(unittest.TestCase):
    def test_header_is_skipped(self):
        rows = decoder.parse_csv("设备号,帧号,固件号,原始字\nD1,1,FW1,3F800000")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["device_id"], "D1")

    def test_english_header_is_skipped(self):
        rows = decoder.parse_csv("device,frame,firmware,raw\nD1,1,FW1,3F800000")
        self.assertEqual(len(rows), 1)


class PayloadSharingTests(unittest.TestCase):
    """Same (firmware, raw word) is one payload shared by many frames."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(
            {
                "TESTING": True,
                "DATA_DIR": self.tmp.name,
                "DATABASE": os.path.join(self.tmp.name, "test.sqlite3"),
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, text):
        data = {"csv": (io.BytesIO(text.encode("utf-8")), "frames.csv")}
        return self.client.post(
            "/upload", data=data, content_type="multipart/form-data"
        )

    def register(self, fw, order):
        return self.client.post(
            "/firmware", data={"fw_version": fw, "word_order": order}
        )

    def frames(self, device):
        with self.app.app_context():
            return service.device_frames(db.get_db(), device)

    def counts(self):
        with self.app.app_context():
            conn = db.get_db()
            return (
                conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM payloads").fetchone()[0],
            )

    def test_identical_payloads_are_stored_once_across_devices(self):
        # Two devices repeatedly report the same measurement word.
        self.register("FW1", "high_first")
        self.upload(
            "D1,1,FW1,3F800000\n"
            "D1,2,FW1,3F800000\n"
            "D2,1,FW1,3F800000\n"
            "D2,2,FW1,40000000"
        )
        frame_count, payload_count = self.counts()
        self.assertEqual(frame_count, 4)
        self.assertEqual(payload_count, 2)
        # Every frame still returns its own raw word, status, reading and time.
        for device in ("D1", "D2"):
            for f in self.frames(device):
                self.assertEqual(f["raw_word"], "3F800000") if f["frame_no"] == 1 else None
                self.assertEqual(f["status"], "decoded")
                self.assertIsNotNone(f["received_at"])
        self.assertEqual(self.frames("D2")[1]["reading"], 2.0)

    def test_index_shows_frame_and_payload_counts(self):
        self.upload(
            "D1,1,FW1,3F800000\nD1,2,FW1,3F800000\nD2,1,FW1,40000000"
        )
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("实际帧数 <strong>3</strong>", html)
        self.assertIn("载荷份数 <strong>2</strong>", html)

    def test_pending_payload_decoded_once_at_registration(self):
        # Six frames (three devices) share two pending payloads.
        self.upload(
            "D1,1,FW1,3F800000\nD1,2,FW1,7FC00000\n"
            "D2,1,FW1,3F800000\nD2,2,FW1,7FC00000\n"
            "D3,1,FW1,3F800000\nD3,2,FW1,7FC00000"
        )
        calls = []
        original = decoder.decode

        def counting_decode(raw_word, order):
            calls.append(raw_word)
            return original(raw_word, order)

        with self.app.app_context():
            conn = db.get_db()
            with unittest.mock.patch.object(decoder, "decode", counting_decode):
                result = service.register_firmware(conn, "FW1", "high_first")
        # Two distinct payloads -> exactly two decode calls, not six.
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["backfilled"], 2)
        self.assertEqual(result["frames_backfilled"], 6)
        frame_count, payload_count = self.counts()
        self.assertEqual(frame_count, 6)
        self.assertEqual(payload_count, 2)

    def test_backfill_writes_payload_layer_and_backfill_time_on_frames(self):
        self.upload("D1,1,FW1,3F800000\nD2,1,FW1,3F800000")
        before = self.frames("D1")[0]["received_at"]
        time.sleep(0.02)
        self.register("FW1", "high_first")
        f1, f2 = self.frames("D1")[0], self.frames("D2")[0]
        # Shared result, and every referencing frame shows this backfill time.
        self.assertEqual(f1["reading"], 1.0)
        self.assertEqual(f2["reading"], 1.0)
        self.assertEqual(f1["decoded_at"], f2["decoded_at"])
        self.assertIsNotNone(f1["decoded_at"])
        self.assertNotEqual(f1["decoded_at"], before)
        # Reception time of the original frames is untouched.
        self.assertEqual(f1["received_at"], before)

    def test_new_frame_reusing_payload_times_decode_at_reception(self):
        self.register("FW1", "high_first")
        self.upload("D1,1,FW1,3F800000")
        first = self.frames("D1")[0]
        time.sleep(0.02)
        # A different device later reuses the already decoded payload.
        self.upload("D2,1,FW1,3F800000")
        second = self.frames("D2")[0]
        self.assertEqual(second["reading"], first["reading"])
        self.assertEqual(second["status"], "decoded")
        self.assertNotEqual(second["decoded_at"], first["decoded_at"])
        self.assertEqual(second["decoded_at"], second["received_at"])
        _, payload_count = self.counts()
        self.assertEqual(payload_count, 1)

    def test_retransmission_keeps_original_frame_times(self):
        self.register("FW1", "high_first")
        self.upload("D1,1,FW1,3F800000")
        first = self.frames("D1")[0]
        time.sleep(0.02)
        self.upload("D1,1,FW1,3F800000")
        again = self.frames("D1")[0]
        self.assertEqual(again["received_at"], first["received_at"])
        self.assertEqual(again["decoded_at"], first["decoded_at"])
        self.assertEqual(self.counts(), (1, 1))

    def test_idempotent_registration_does_not_touch_backfill_time(self):
        self.upload("D1,1,FW1,3F800000")
        self.register("FW1", "high_first")
        first = self.frames("D1")[0]["decoded_at"]
        time.sleep(0.02)
        self.register("FW1", "high_first")
        self.assertEqual(self.frames("D1")[0]["decoded_at"], first)

    def test_distinct_raw_words_are_distinct_payloads(self):
        self.register("FW1", "high_first")
        self.upload("D1,1,FW1,3F800000\nD1,2,FW1,40000000")
        self.assertEqual(self.counts(), (2, 2))

    def test_same_raw_word_different_firmware_is_distinct_payload(self):
        self.register("FW1", "high_first")
        self.register("FW2", "low_first")
        self.upload("D1,1,FW1,00003F80\nD1,2,FW2,00003F80")
        frame_count, payload_count = self.counts()
        self.assertEqual(frame_count, 2)
        self.assertEqual(payload_count, 2)
        # Same raw word decodes differently per registered word order.
        frames = {f["frame_no"]: f for f in self.frames("D1")}
        # high_first on 00003F80 is a tiny denormal, low_first yields 1.0.
        self.assertAlmostEqual(frames[2]["reading"], 1.0)

    def test_new_frame_reusing_pending_payload_stays_pending(self):
        # Unknown firmware: a second frame reusing the same pending payload
        # must not invent a reading or a decode time.
        self.upload("D1,1,FW1,3F800000")
        self.upload("D2,1,FW1,3F800000")
        f = self.frames("D2")[0]
        self.assertEqual(f["status"], "pending")
        self.assertIsNone(f["reading"])
        self.assertIsNone(f["decoded_at"])
        self.assertIsNotNone(f["received_at"])
        self.assertEqual(self.counts(), (2, 1))

    def test_intra_batch_payload_is_shared(self):
        # One batch with the same payload repeated across devices creates one
        # payload row and multiple frame rows in a single transaction.
        resp = self.client.post(
            "/api/upload",
            data=(
                "D1,1,FW1,3F800000\n"
                "D2,1,FW1,3F800000\n"
                "D3,1,FW1,3F800000\n"
            ),
            content_type="text/csv",
        )
        summary = resp.get_json()["summary"]
        self.assertEqual(summary["inserted"], 3)
        self.assertEqual(summary["new_payloads"], 1)
        self.assertEqual(self.counts(), (3, 1))

    def test_upload_summary_reports_new_payloads(self):
        resp = self.client.post(
            "/api/upload",
            data="D1,1,FW1,3F800000\nD2,1,FW1,3F800000\nD1,2,FW1,40000000\n",
            content_type="text/csv",
        )
        summary = resp.get_json()["summary"]
        self.assertEqual(summary["inserted"], 3)
        self.assertEqual(summary["new_payloads"], 2)
        self.assertEqual(summary["pending"], 3)

    def test_api_frame_shape_unchanged(self):
        self.register("FW1", "high_first")
        self.upload("D1,1,FW1,3F800000")
        body = self.client.get("/api/devices/D1").get_json()
        f = body["frames"][0]
        for field in (
            "device_id", "frame_no", "fw_version", "raw_word", "status",
            "reading", "word_order", "bit_pattern", "received_at", "decoded_at",
        ):
            self.assertIn(field, f)
        self.assertEqual(f["reading"], 1.0)
        self.assertEqual(f["raw_word"], "3F800000")


class SchemaMigrationTests(unittest.TestCase):
    """Existing data volumes upgrade automatically and losslessly."""

    LEGACY_SCHEMA = """
    CREATE TABLE firmware (
        fw_version   TEXT PRIMARY KEY,
        word_order   TEXT NOT NULL CHECK (word_order IN ('high_first', 'low_first')),
        registered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    CREATE TABLE frames (
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
    CREATE INDEX idx_frames_fw_status ON frames (fw_version, status);
    CREATE INDEX idx_frames_device ON frames (device_id, frame_no);
    """

    def _build_legacy_db(self, path):
        legacy = sqlite3.connect(path)
        try:
            legacy.executescript(self.LEGACY_SCHEMA)
            legacy.execute(
                "INSERT INTO firmware VALUES ('FW1', 'high_first', '2026-01-01T00:00:00.000Z')"
            )
            legacy.executemany(
                "INSERT INTO frames (id, device_id, frame_no, fw_version, raw_word,"
                " status, reading, word_order, bit_pattern, received_at, decoded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (1, "D1", 1, "FW1", "3F800000", "decoded", 1.0,
                     "high_first", "3F800000",
                     "2026-01-02T00:00:00.000Z", "2026-01-02T00:00:00.000Z"),
                    (2, "D1", 2, "FW1", "3F800000", "decoded", 1.0,
                     "high_first", "3F800000",
                     "2026-01-03T00:00:00.000Z", "2026-01-03T00:00:00.000Z"),
                    (3, "D2", 1, "FWX", "7FC00000", "pending", None,
                     None, None, "2026-01-04T00:00:00.000Z", None),
                ],
            )
            legacy.commit()
        finally:
            legacy.close()

    def test_legacy_volume_upgrades_and_preserves_every_frame(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = os.path.join(tmp.name, "legacy.sqlite3")
        self._build_legacy_db(db_path)

        app = create_app(
            {"TESTING": True, "DATA_DIR": tmp.name, "DATABASE": db_path}
        )
        with app.app_context():
            conn = db.get_db()
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], db.USER_VERSION
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0], 3
            )
            # The two identical frames now share one payload.
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM payloads").fetchone()[0], 2
            )
            # Original per-frame fields and values survive.
            rows = service.device_frames(conn, "D1")
            self.assertEqual([r["frame_no"] for r in rows], [1, 2])
            for r in rows:
                self.assertEqual(r["fw_version"], "FW1")
                self.assertEqual(r["raw_word"], "3F800000")
                self.assertEqual(r["status"], "decoded")
                self.assertEqual(r["reading"], 1.0)
                self.assertEqual(r["bit_pattern"], "3F800000")
                self.assertEqual(r["word_order"], "high_first")
            self.assertEqual(
                rows[0]["received_at"], "2026-01-02T00:00:00.000Z"
            )
            self.assertEqual(
                rows[1]["received_at"], "2026-01-03T00:00:00.000Z"
            )
            pending = service.device_frames(conn, "D2")[0]
            self.assertEqual(pending["status"], "pending")
            self.assertIsNone(pending["reading"])

        # Restarting again is a no-op, not a second migration.
        app2 = create_app(
            {"TESTING": True, "DATA_DIR": tmp.name, "DATABASE": db_path}
        )
        with app2.app_context():
            conn = db.get_db()
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0], 3
            )

    def test_migration_failure_keeps_legacy_data_and_retries(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = os.path.join(tmp.name, "legacy.sqlite3")
        self._build_legacy_db(db_path)

        original = db._migrate_legacy

        def failing_migration(conn):
            original(conn)
            raise RuntimeError("simulated crash after migration statements")

        with unittest.mock.patch.object(db, "_migrate_legacy", failing_migration):
            with self.assertRaises(RuntimeError):
                create_app(
                    {"TESTING": True, "DATA_DIR": tmp.name, "DATABASE": db_path}
                )

        # The transaction rolled back: old wide table is fully intact.
        legacy = sqlite3.connect(db_path)
        try:
            self.assertEqual(
                legacy.execute("SELECT COUNT(*) FROM frames").fetchone()[0], 3
            )
            tables = {
                r[0]
                for r in legacy.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertNotIn("payloads", tables)
            self.assertEqual(
                legacy.execute("PRAGMA user_version").fetchone()[0], 0
            )
        finally:
            legacy.close()

        # A retry on the next startup succeeds.
        app2 = create_app(
            {"TESTING": True, "DATA_DIR": tmp.name, "DATABASE": db_path}
        )
        with app2.app_context():
            conn = db.get_db()
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0], 3
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM payloads").fetchone()[0], 2
            )


if __name__ == "__main__":
    unittest.main()
