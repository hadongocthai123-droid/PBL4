# SmartDoor AIoT Server

Chạy trên Windows: nhấp đúp `run.bat` (hoặc `start_server.bat` ở thư mục gốc). Script tự tạo `.venv` bằng Python 3.12 (nếu có), cài `requirements.txt`, chạy Uvicorn trên `0.0.0.0:8000` và mở trình duyệt tại `http://127.0.0.1:8000/`.

## Mô hình AI (`models/Model 1` ... `Model 4`)

- **Model 1 (`best.onnx`):** YOLOv8n phát hiện khuôn mặt (input `320x320`).
- **Model 2 (`rec_arcface.onnx`, `face_db.npy`):** MobileNetV3-Large + ArcFace trích xuất vector 512 chiều (`112x112`).
- **Model 3 (`antispoof.onnx` — tùy chọn):** EfficientNet-B0 chống giả mạo (`224x224`, output softmax). Nếu không có file model, server tự động bypass an toàn và hỗ trợ nạp lại nóng qua nút **Nạp lại Model**.
- **Model 4 (`speaker_arcface.onnx`, `voice_db.npy`):** Speaker verification 256 chiều từ Log-Mel Spectrogram `(64, 301)` (`sample_rate=16000`, `n_fft=512`, `win_length=400`, `hop_length=160`, HTK Mel filterbank khớp pipeline huấn luyện).

## Endpoint chính cho ESP32-CAM & Dashboard

- `WS /ws/esp32`: Nhận JPEG binary (hoặc JSON `FRAME`/`HEARTBEAT`) từ ESP32-CAM, đồng thời gửi lệnh `DOOR_COMMAND` (`UNLOCK`/`LOCK`) và `ALERT`. Khi kết nối WebSocket hoạt động, bridge tự động dừng HTTP polling để tránh nghẽn và tranh chấp frame.
- `WS /ws/esp32-cam`: Kênh chỉ nhận JPEG binary thuần.
- `WS /ws/esp32-control`: Kênh điều khiển & cảm biến từ cho ESP32-S3.
- `WS /ws`: Kênh đẩy khung hình đã vẽ/gắn bounding box, FPS, telemetry và sự kiện lên Web Dashboard.
- `GET /api/camera/snapshot.jpg`: Lấy ảnh JPEG mới nhất từ camera hiện tại để kiểm tra kết nối nhanh.

## Biến môi trường tùy chọn

`SMARTDOOR_HOST`, `SMARTDOOR_PORT`, `SMARTDOOR_MODEL_ROOT`, `ESP32_CAM_URL`, `SMARTDOOR_FRAME_STALE`, `SMARTDOOR_MAX_CAPTURES`, `SMARTDOOR_API_KEY`, `SMARTDOOR_DEVICE_TOKEN`.
