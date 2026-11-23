"""Automated tests for SmartDoor AIoT backend, Mel Spectrogram pipeline, and ESP32-CAM bridge."""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

HAS_NUMPY = importlib.util.find_spec("numpy") is not None
HAS_CV2 = importlib.util.find_spec("cv2") is not None
HAS_FASTAPI = importlib.util.find_spec("fastapi") is not None


class TestDatabase(unittest.TestCase):
    def setUp(self):
        import database

        self.database = database
        self.tmpdir = tempfile.TemporaryDirectory()
        self._orig_db = database.DB_PATH
        database.DB_PATH = Path(self.tmpdir.name) / "test.db"
        database.init_db()

    def tearDown(self):
        self.database.DB_PATH = self._orig_db
        self.tmpdir.cleanup()

    def test_ensure_and_upsert_members(self):
        self.database.ensure_member("person-001")
        self.assertEqual(self.database.get_member_name("person-001"), "person-001")

        self.database.upsert_member("person-001", "Nguyễn Văn A")
        self.assertEqual(self.database.get_member_name("person-001"), "Nguyễn Văn A")

        # ensure_member không được ghi đè tên hiển thị đã đặt
        self.database.ensure_member("person-001", "person-001")
        self.assertEqual(self.database.get_member_name("person-001"), "Nguyễn Văn A")

        self.database.delete_member("person-001")
        self.assertEqual(self.database.list_members(), [])

    def test_add_and_query_events(self):
        eid = self.database.add_event("Mở cửa thành công", "person-001", "Nguyễn Văn A", 0.88, 0.96, None, "captures/1.jpg")
        self.assertIsNotNone(eid)
        rows = self.database.recent_events(limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["display_name"], "Nguyễn Văn A")
        self.assertAlmostEqual(rows[0]["face_score"], 0.88, places=4)


@unittest.skipUnless(HAS_NUMPY and HAS_CV2, "Cần numpy và opencv-python")
class TestAIEngineAndBridge(unittest.TestCase):
    def test_htk_mel_filterbank_covers_full_spectrum(self):
        import numpy as np
        from ai_engine import AIEngine, _build_hann_window, _build_htk_mel_filterbank

        win = _build_hann_window(win_length=400, n_fft=512)
        self.assertEqual(win.shape, (512,))
        self.assertEqual(float(np.sum(win[:56])), 0.0)
        self.assertEqual(float(np.sum(win[-56:])), 0.0)
        self.assertGreater(float(win[256]), 0.99)

        banks = _build_htk_mel_filterbank(sample_rate=16000, n_fft=512, n_mels=64)
        self.assertEqual(banks.shape, (64, 257))
        # Kiểm tra dải tần cao (4000-7500 Hz -> bin 128..240) có bộ lọc hoạt động (sửa lỗi cũ bị bằng 0 trên 2840 Hz)
        self.assertGreater(float(banks[:, 128:240].sum()), 5.0)
        # Filter cuối cùng phải nằm gần Nyquist 8000 Hz
        self.assertGreater(int(np.argmax(banks[-1])), 230)

        engine = AIEngine(load_models=False)
        for duration_samples in (16000, 48000, 72000):
            t = np.linspace(0, duration_samples / 16000.0, duration_samples, dtype=np.float32)
            tone = 0.5 * np.sin(2 * np.pi * 440.0 * t) + 0.3 * np.sin(2 * np.pi * 4500.0 * t)
            mel = engine.extract_log_mel(tone, 16000)
            self.assertEqual(mel.shape, (64, 301))
            self.assertTrue(np.isfinite(mel).all())

    def test_esp32_bridge_seq_and_source_isolation(self):
        import cv2
        import numpy as np
        from esp32_bridge import ESP32Bridge

        bridge = ESP32Bridge("http://192.168.1.100:81/stream", start_capture_thread=False)
        bridge.set_source("esp32")

        img = np.zeros((240, 320, 3), dtype=np.uint8)
        cv2.circle(img, (160, 120), 40, (255, 255, 255), -1)
        ok, jpg = cv2.imencode(".jpg", img)
        self.assertTrue(ok)

        self.assertTrue(bridge.decode_and_push_jpeg(jpg.tobytes(), source="esp32", transport="websocket"))
        self.assertTrue(bridge.has_frame())

        frame1, seq1 = bridge.get_frame(after_seq=-1)
        self.assertIsNotNone(frame1)
        self.assertEqual(frame1.shape, (240, 320, 3))
        self.assertEqual(bridge.get_telemetry()["resolution"], "320x240")
        self.assertEqual(bridge.get_telemetry()["transport"], "websocket")

        # Không được trả lại cùng frame khi gọi với after_seq=seq1
        frame_dup, seq_dup = bridge.get_frame(after_seq=seq1)
        self.assertIsNone(frame_dup)
        self.assertEqual(seq_dup, seq1)

        # Đổi sang webcam thì không trả về frame cũ của esp32
        bridge.set_source("webcam")
        self.assertFalse(bridge.has_frame())


@unittest.skipUnless(HAS_NUMPY and HAS_CV2 and HAS_FASTAPI, "Cần fastapi, numpy, opencv-python")
class TestFastAPIIntegration(unittest.TestCase):
    def test_esp32_ws_frame_and_2fa_protection(self):
        import cv2
        import numpy as np
        from fastapi.testclient import TestClient
        import app as server_app

        img = np.full((240, 320, 3), 120, dtype=np.uint8)
        ok, jpg = cv2.imencode(".jpg", img)
        self.assertTrue(ok)

        with TestClient(server_app.app) as client:
            # 1. Verify 2FA không được phép bypass khi chưa có voice_pending
            resp = client.post("/api/verify-voice", json={"user_id": "person-001", "samples": [0.1] * 100, "sample_rate": 16000})
            self.assertEqual(resp.status_code, 400)

            # 2. Giả lập ESP32-CAM kết nối /ws/esp32, gửi heartbeat và JPEG binary
            with client.websocket_connect("/ws/esp32") as ws:
                ws.send_json({"event": "HEARTBEAT", "device": "esp32-cam", "door": "locked", "rssi": -50, "ip": "192.168.1.55", "camera_ready": True})
                ws.send_bytes(jpg.tobytes())

                st = client.get("/api/status").json()
                self.assertEqual(st["camera_source"], "esp32")
                self.assertTrue(st["camera_has_frame"])
                self.assertTrue(st["telemetry"]["cam_online"])
                self.assertEqual(st["telemetry"]["transport"], "websocket")
                self.assertEqual(st["telemetry"]["ip"], "192.168.1.55")

                snap = client.get("/api/camera/snapshot.jpg")
                self.assertEqual(snap.status_code, 200)
                self.assertEqual(snap.headers["content-type"], "image/jpeg")


if __name__ == "__main__":
    unittest.main(verbosity=2)
