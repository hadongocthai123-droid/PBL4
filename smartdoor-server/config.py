"""Application settings for SmartDoor AIoT."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CAPTURES_DIR = STATIC_DIR / "captures"
CAMERA_SETTINGS_PATH = BASE_DIR / "camera_settings.json"
THRESHOLDS_SETTINGS_PATH = BASE_DIR / "thresholds_settings.json"

MODEL_ROOT = Path(os.getenv("SMARTDOOR_MODEL_ROOT", str(BASE_DIR / "models")))
MODEL1_PATH = MODEL_ROOT / "Model 1" / "best.onnx"
MODEL2_PATH = MODEL_ROOT / "Model 2" / "rec_arcface.onnx"
FACE_DB_PATH = MODEL_ROOT / "Model 2" / "face_db.npy"
MODEL3_PATH = MODEL_ROOT / "Model 3" / "antispoof.onnx"
MODEL4_PATH = MODEL_ROOT / "Model 4" / "speaker_arcface.onnx"
VOICE_DB_PATH = MODEL_ROOT / "Model 4" / "voice_db.npy"

HOST = os.getenv("SMARTDOOR_HOST", "0.0.0.0")
PORT = int(os.getenv("SMARTDOOR_PORT", "8000"))
ESP32_CAM_URL = os.getenv("ESP32_CAM_URL", "http://192.168.1.100:81/stream")

# Ngưỡng AI mặc định (có thể tinh chỉnh trên giao diện và lưu tự động)
FACE_THRESHOLD = 0.58
ANTISPOOF_THRESHOLD = 0.55
VOICE_THRESHOLD = 0.38
DETECTION_THRESHOLD = 0.50
NMS_THRESHOLD = 0.45

# Tham số vận hành cửa & hệ thống
UNLOCK_DURATION = 5
UNLOCK_COOLDOWN = 6
VOICE_TIMEOUT = 8
SPOOF_ALERT_COOLDOWN = float(os.getenv("SMARTDOOR_SPOOF_COOLDOWN", "5.0"))
FRAME_STALE_SECONDS = float(os.getenv("SMARTDOOR_FRAME_STALE", "5.0"))
MAX_CAPTURE_FILES = int(os.getenv("SMARTDOOR_MAX_CAPTURES", "500"))

# Bảo mật tùy chọn khi triển khai trên mạng LAN dùng chung (để trống = tắt cho chế độ demo nội bộ)
API_KEY = os.getenv("SMARTDOOR_API_KEY", "").strip()
DEVICE_TOKEN = os.getenv("SMARTDOOR_DEVICE_TOKEN", "").strip()
