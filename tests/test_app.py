import io
import os
import struct
import sys
import tempfile
import time
import unittest

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
                "SELECT frame_no, status, reading, raw_word FROM frames ORDER BY frame_no"
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
        self.assertEqual(sum(r["backfilled"] for r in results), 9)
        with self.app.app_context():
            conn = db.get_db()
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM frames WHERE status='pending'"
                ).fetchone()[0],
                0,
            )


class HeaderTests(unittest.TestCase):
    def test_header_is_skipped(self):
        rows = decoder.parse_csv("设备号,帧号,固件号,原始字\nD1,1,FW1,3F800000")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["device_id"], "D1")

    def test_english_header_is_skipped(self):
        rows = decoder.parse_csv("device,frame,firmware,raw\nD1,1,FW1,3F800000")
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
