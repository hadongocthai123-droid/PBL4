"""Camera sources (Webcam, ESP32-CAM WebSocket, ESP32 HTTP fallback) and device WebSocket bridge."""
import json
import threading
import time
import urllib.request
from urllib.parse import urlsplit

import cv2
import numpy as np

import config


class ESP32Bridge:
    def __init__(self, url, start_capture_thread=True):
        self.url = url
        self.source = "webcam"
        self.latest = None
        self.latest_source = None
        self.latest_at = None
        self.frame_seq = 0
        self.last_ws_frame_at = 0.0
        self.lock = threading.Lock()

        self.esp_socket = None         # ESP32-CAM (/ws/esp32)
        self.esp_cam_socket = None     # ESP32-CAM raw stream (/ws/esp32-cam)
        self.controller_socket = None  # ESP32-S3 (/ws/esp32-control)

        self.telemetry = {
            "door": "unknown",
            "rssi": None,
            "online": False,
            "cam_online": False,
            "controller_online": False,
            "transport": "none",
            "ip": None,
            "camera_ready": None,
            "frames_received": 0,
            "resolution": None,
        }

        self.running = True
        self.thread = None
        if start_capture_thread:
            self.thread = threading.Thread(target=self._capture_loop, daemon=True)
            self.thread.start()

    def set_source(self, source, url=None, clear_frame=True):
        if source not in ("webcam", "esp32"):
            raise ValueError("Nguồn camera không hợp lệ (chỉ chấp nhận 'webcam' hoặc 'esp32')")
        with self.lock:
            changed = (source != self.source) or (url is not None and url != self.url)
            self.source = source
            if url:
                self.url = url.strip()
            if clear_frame and changed:
                self.latest = None
                self.latest_source = None
                self.latest_at = None
            self._refresh_transport_locked()

    def _is_ws_cam_active_locked(self):
        return (
            self.esp_socket is not None
            or self.esp_cam_socket is not None
            or (time.monotonic() - self.last_ws_frame_at <= config.FRAME_STALE_SECONDS)
        )

    def _refresh_transport_locked(self):
        cam_ws = self.esp_socket is not None or self.esp_cam_socket is not None
        ctrl_ws = self.controller_socket is not None
        self.telemetry["cam_online"] = cam_ws
        self.telemetry["controller_online"] = ctrl_ws
        self.telemetry["online"] = bool(cam_ws or ctrl_ws)
        if self.source == "webcam":
            self.telemetry["transport"] = "webcam"
        elif cam_ws:
            self.telemetry["transport"] = "websocket"
        elif self.url:
            self.telemetry["transport"] = "http"
        else:
            self.telemetry["transport"] = "none"

    def refresh_online_state(self):
        with self.lock:
            self._refresh_transport_locked()
            return dict(self.telemetry)

    def update_telemetry(self, data, device_hint=None):
        device = data.get("device") or device_hint or "esp32-cam"
        with self.lock:
            if "rssi" in data and data["rssi"] is not None:
                self.telemetry["rssi"] = data["rssi"]
            if "ip" in data and data["ip"]:
                self.telemetry["ip"] = data["ip"]
            if "camera_ready" in data and data["camera_ready"] is not None:
                self.telemetry["camera_ready"] = bool(data["camera_ready"])

            # Nếu có ESP32-S3 (có cảm biến từ MC-38), ưu tiên trạng thái cửa vật lý của S3
            # để ESP32-CAM không ghi đè mất trạng thái open/closed.
            if "door" in data and data["door"] is not None:
                if device == "esp32-s3" or not self.controller_socket:
                    self.telemetry["door"] = data["door"]

            self._refresh_transport_locked()
            return dict(self.telemetry)

    def push_frame(self, frame, source="esp32", transport="websocket"):
        if frame is None or getattr(frame, "size", 0) == 0:
            return False
        now = time.monotonic()
        h, w = frame.shape[:2]
        with self.lock:
            if transport == "websocket":
                self.last_ws_frame_at = now
                self.telemetry["frames_received"] += 1
                self.telemetry["resolution"] = f"{w}x{h}"
            # Chỉ cập nhật frame hiển thị/suy luận nếu đúng nguồn đang được chọn
            if source != self.source:
                return False
            self.latest = frame
            self.latest_source = source
            self.latest_at = now
            self.frame_seq += 1
            self.telemetry["resolution"] = f"{w}x{h}"
            self.telemetry["transport"] = transport
            return True

    def decode_and_push_jpeg(self, raw_bytes, source="esp32", transport="websocket"):
        if not raw_bytes:
            return False
        arr = np.frombuffer(raw_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            return False
        return self.push_frame(frame, source=source, transport=transport)

    def _capture_loop(self):
        cap = None
        active = None  # tuple (source, target_url_or_0)
        while self.running:
            with self.lock:
                source = self.source
                url = self.url
                ws_cam_active = self._is_ws_cam_active_locked()

            # Khi ESP32-CAM đang truyền frame qua WebSocket (/ws/esp32 hoặc /ws/esp32-cam),
            # tuyệt đối không mở HTTP polling / VideoCapture song song gây nghẽn và tranh chấp frame.
            if source == "esp32" and ws_cam_active:
                if cap is not None:
                    cap.release()
                    cap = None
                    active = None
                time.sleep(0.2)
                continue

            if source == "esp32" and url.rstrip("/").endswith(":81/stream"):
                if cap is not None:
                    cap.release()
                    cap = None
                    active = None
                try:
                    parts = urlsplit(url)
                    capture_url = f"{parts.scheme}://{parts.hostname}/capture?_cb={int(time.time() * 1000)}"
                    with urllib.request.urlopen(capture_url, timeout=2.5) as response:
                        data = response.read()
                    self.decode_and_push_jpeg(data, source="esp32", transport="http")
                    time.sleep(0.12)
                except Exception:
                    time.sleep(0.6)
                continue

            target = 0 if source == "webcam" else url
            desired = (source, target)
            if desired != active:
                if cap is not None:
                    cap.release()
                    cap = None
                if target != "":
                    cap = cv2.VideoCapture(target)
                active = desired

            if cap is not None and cap.isOpened():
                ok, frame = cap.read()
                if ok and frame is not None:
                    self.push_frame(frame, source=source, transport="webcam" if source == "webcam" else "http")
                    time.sleep(0.01)
                else:
                    cap.release()
                    cap = None
                    active = None
                    time.sleep(1.2)
            else:
                if cap is not None:
                    cap.release()
                cap = None
                active = None
                time.sleep(1.5)

        if cap is not None:
            cap.release()

    def get_frame(self, after_seq=None):
        """Trả về (frame_copy, frame_seq). Nếu chưa có frame mới hơn after_seq thì trả (None, frame_seq)."""
        with self.lock:
            if (
                self.latest is None
                or self.latest_at is None
                or self.latest_source != self.source
                or (time.monotonic() - self.latest_at) > config.FRAME_STALE_SECONDS
            ):
                return None, self.frame_seq
            if after_seq is not None and self.frame_seq == after_seq:
                return None, self.frame_seq
            return self.latest.copy(), self.frame_seq

    def frame(self):
        frame, _ = self.get_frame(after_seq=None)
        return frame

    def has_frame(self):
        with self.lock:
            return (
                self.latest is not None
                and self.latest_at is not None
                and self.latest_source == self.source
                and (time.monotonic() - self.latest_at) <= config.FRAME_STALE_SECONDS
            )

    def frame_source(self):
        with self.lock:
            if (
                self.latest is not None
                and self.latest_at is not None
                and (time.monotonic() - self.latest_at) <= config.FRAME_STALE_SECONDS
            ):
                return self.latest_source
            return None

    def get_telemetry(self):
        with self.lock:
            return dict(self.telemetry)

    async def send_command(self, payload):
        sent = False
        encoded = json.dumps(payload)
        for name in ("esp_socket", "controller_socket"):
            ws = getattr(self, name)
            if ws is not None:
                try:
                    await ws.send_text(encoded)
                    sent = True
                except Exception:
                    setattr(self, name, None)
        self.refresh_online_state()
        return sent
