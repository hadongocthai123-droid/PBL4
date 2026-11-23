# SmartDoor AIoT — Hướng dẫn Cài đặt & Vận hành

Hệ thống khóa cửa thông minh AIoT gồm:
- **ESP32-CAM** (`firmware/esp32_cam_smartdoor/esp32_cam_smartdoor.ino`): chụp JPEG QVGA (`320x240`), stream qua WebSocket `/ws/esp32` lên server để AI xử lý và điều khiển relay.
- **SmartDoor Server** (`smartdoor-server/`): FastAPI + ONNX Runtime chạy 4 mô hình AI (Model 1: Face Detection, Model 2: Face Recognition, Model 3: Anti-Spoof tùy chọn, Model 4: Speaker Verification) + Web Dashboard trực tiếp.
- **ESP32-S3** (`sketch_nov9a/sketch_nov9a.ino` — tùy chọn): điều khiển chốt Servo, cảm biến HC-SR04/MC-38, nút mở trong, còi và loa MP3 qua `/ws/esp32-control`.

---

## 1. Yêu cầu môi trường

- **Máy tính chạy Server (Windows 10/11):**
  - **Python 3.12 (64-bit)** (khuyến nghị, vì `onnxruntime` và `opencv-python` tương thích ổn định nhất; khi cài nhớ tick **Add python.exe to PATH**).
- **Môi trường nạp Firmware:**
  - **Arduino IDE 2.x** + Board package **esp32 by Espressif Systems (bản 3.x)**.
  - 2 thư viện cài qua Library Manager:
    1. **WebSockets** (tác giả *Markus Sattler*)
    2. **ArduinoJson** (bản **7.x**)
- **Mạng:** Máy tính và ESP32-CAM kết nối chung một mạng Wi-Fi **2.4 GHz** (ESP32 không bắt được Wi-Fi 5 GHz).

---

## 2. Chạy Backend Server & Web Dashboard

### Cách 1: Chạy tự động (Khuyến nghị)
1. Nhấp đúp file `start_server.bat` ở thư mục gốc (hoặc `smartdoor-server\run.bat`).
2. Script sẽ tự động:
   - Tạo môi trường ảo `smartdoor-server\.venv`.
   - Cài toàn bộ thư viện trong `smartdoor-server\requirements.txt`.
   - In ra danh sách **địa chỉ IPv4 LAN** của máy tính (ví dụ: `192.168.1.15`) → **ghi lại địa chỉ này** để điền vào `wifi_config.h`.
   - Chạy server tại cổng `8000` và tự mở trình duyệt `http://127.0.0.1:8000/`.

### Cách 2: Chạy thủ công bằng PowerShell (nếu muốn tự kiểm soát log)
```powershell
cd c:\Users\hadon\PBL4\smartdoor-server
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```
Sau đó mở trình duyệt vào `http://127.0.0.1:8000/` (không nhập `0.0.0.0` lên trình duyệt).

### Mở Windows Firewall cho cổng 8000 (Rất quan trọng!)
Nếu Windows Defender Firewall hỏi quyền mạng khi chạy Uvicorn lần đầu, chọn **Allow access (Private networks)**. Hoặc mở PowerShell quyền **Administrator** chạy lệnh:
```powershell
New-NetFirewallRule -DisplayName "SmartDoor AIoT 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
```

---

## 3. Cấu hình Wi-Fi & Nạp firmware ESP32-CAM

### Bước 1: Tạo file `wifi_config.h`
Copy file `wifi_config.example.h` ở thư mục gốc thành `wifi_config.h` (file này đã nằm trong `.gitignore` để tránh lộ mật khẩu Wi-Fi khi push Git) và điền thông tin mạng thực tế:
```cpp
#pragma once

#define WIFI_SSID "TEN_WIFI_2_4GHZ"
#define WIFI_PASSWORD "MAT_KHAU_WIFI"
#define SERVER_IP "192.168.1.15"   // Thay bằng IPv4 LAN máy tính chạy server
#define SERVER_PORT 8000
```

### Bước 2: Nạp code bằng Arduino IDE 2
1. Mở `firmware\esp32_cam_smartdoor\esp32_cam_smartdoor.ino`.
2. Trong menu **Tools**, chọn đúng cấu hình:
   - **Board:** `AI Thinker ESP32-CAM`
   - **PSRAM:** `Enabled` *(nếu board không có PSRAM, firmware vẫn tự fallback về DRAM)*
   - **Port:** Cổng `COM` của mạch nạp USB-to-Serial (FTDI / CH340 / đế nạp MB).
3. Nếu nạp qua mạch FTDI rời: nối **`IO0` ↔ `GND`** rồi nhấn nút Reset để vào chế độ nạp → bấm **Upload**.
4. Nạp xong: **tháo dây `IO0` khỏi `GND`** và nhấn nút Reset trên board.

### Bước 3: Kiểm tra luồng hình ảnh ESP32-CAM → Server → Web
1. Mở **Serial Monitor** ở tốc độ **115200 baud**. Khi hoạt động bình thường, mỗi 5 giây sẽ thấy:
   ```text
   [WiFi] Da ket noi! IP: 192.168.1.42 | RSSI: -48 dBm
   [WS] Da ket noi toi ws://192.168.1.15:8000/ws/esp32
   [STAT] WiFi=OK | WS=OK | Cam=OK | Sent=50 | Drop=0 | Last=7842 B | RSSI=-48
   ```
2. Trên trình duyệt `http://127.0.0.1:8000/`:
   - Đèn góc trên chuyển xanh: **`ESP32 trực tuyến (WS)`**.
   - Nguồn camera tự chuyển sang **ESP32-CAM**, khung hình live hiển thị kèm bounding box nhận diện của Model 1/2/3 và FPS thực tế.
   - Dòng telemetry dưới khung video hiện: `Kênh: ESP32 WebSocket (320x240) · Cửa: locked · Wi-Fi RSSI: -48 dBm · Frames: ...`.
3. Kiểm tra nhanh bằng URL phụ (nếu cần debug):
   - Xem ảnh JPEG mới nhất server nhận được: `http://127.0.0.1:8000/api/camera/snapshot.jpg`
   - Xem JSON trạng thái toàn hệ thống: `http://127.0.0.1:8000/api/status`

---

## 4. Test nhanh các tính năng trên Web Dashboard

- **Test không cần phần cứng:** Chọn nguồn camera là **Laptop Webcam** để kiểm tra Model 1/2/4 trước.
- **Danh sách thành viên sẵn có:** 4 thành viên từ `face_db.npy` (`person-001`..`person-004`) được tự động đồng bộ vào tab **Đăng ký mới → Thành viên hiện có**.
- **Đăng ký thêm khuôn mặt từ ESP32-CAM:** Vào tab **Đăng ký mới** → nhập `Mã người dùng` + `Tên hiển thị` → bấm **Chụp thêm mẫu từ camera đang chọn** nhiều lần ở các góc mặt khác nhau (chính diện, nghiêng trái/phải).
- **Chế độ 2FA (Khuôn mặt + Giọng nói):** Ghi đủ 3 mẫu giọng nói 3 giây cho thành viên trước, sau đó chuyển thanh công cụ sang **2 Lớp: Khuôn mặt + Giọng nói**.
- **Tinh chỉnh ngưỡng:** Kéo các slider ở tab **Tinh chỉnh AI**; thông số tự động lưu vào `thresholds_settings.json` sau mỗi lần đổi.

---

## 5. Chạy bộ Unit Test tự động

Sau khi đã cài `.venv` (ở mục 2), có thể kiểm tra toàn bộ pipeline (SQLite, HTK Mel Spectrogram Model 4, ESP32-CAM WebSocket stream, chống bypass 2FA) bằng lệnh:
```powershell
cd c:\Users\hadon\PBL4\smartdoor-server
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

---

## 6. Đấu dây phần cứng (Tham khảo)

### A. Relay 12V trên ESP32-CAM (nếu điều khiển chốt trực tiếp từ ESP32-CAM)
- `GPIO13` → chân `IN` relay; nối chung `GND`. Đổi `RELAY_ON` (`HIGH`/`LOW`) tùy loại module relay.
- Nguồn 12V (+) → `COM`; `NO` → (+) solenoid; (−) solenoid → 12V (−). Mắc diode dập xung ngược song song cuộn solenoid.
- `GPIO4`: LED flash nháy ngắn khi có cảnh báo giả mạo. `GPIO15`: TX UART gửi `OPEN\n` / `ALARM\n` (không dùng đồng thời thẻ nhớ microSD).

### B. Mạch ESP32-S3 (`sketch_nov9a\sketch_nov9a.ino` — dùng khi tích hợp thêm phần chốt Servo/Cảm biến)
- Dùng chung `wifi_config.h` ở thư mục gốc; hai file âm thanh `sound_data.h` và `sound_data_wrong.h` nằm sẵn trong thư mục `sketch_nov9a/`.
- Sơ đồ chân: Servo `GPIO18`, HC-SR04 `TRIG=7 / ECHO=8` (qua cầu phân áp 1kΩ/2kΩ), MC-38 `GPIO20`, nút bấm trong `GPIO19`, LED `GPIO21` (trở 220Ω), Buzzer `GPIO38`, loa I2S MAX98357A `BCLK=4 / LRC=5 / DIN=6`, mic INMP441 `SCK=14 / WS=15 / SD=16` (`VDD` 3.3V, `L/R` GND).

---

## 7. Xử lý các lỗi thường gặp (Troubleshooting)

| Hiện tượng | Nguyên nhân thường gặp | Cách xử lý |
|---|---|---|
| `[WiFi] Dang thu ket noi lai...` mãi | Sai mật khẩu hoặc đang bắt Wi-Fi 5 GHz | Kiểm tra lại `wifi_config.h`, đổi sang băng tần **2.4 GHz**. |
| `WiFi=OK` nhưng `WS=DOWN` | Sai `SERVER_IP` hoặc Windows Firewall chặn cổng 8000 | Kiểm tra lại IPv4 LAN của máy tính và mở Firewall cổng TCP `8000`. |
| `[CAM] Khoi tao that bai` hoặc board hay tự reset | Cáp camera lỏng hoặc nguồn 5V yếu (brownout) | Cắm chắc lại cáp camera OV2640, cấp nguồn **5V ≥ 1A** ổn định (tránh cấp từ chân 3.3V yếu). |
| Nạp code xong board không chạy | Quên tháo dây `IO0` nối `GND` | Tháo dây `IO0-GND` rồi nhấn nút **Reset**. |
| Hình từ ESP32-CAM bị lộn ngược / gương | Chiều lắp module camera | Trong `esp32_cam_smartdoor.ino`, bỏ comment `s->set_vflip(s, 1);` hoặc `s->set_hmirror(s, 1);` trong hàm `tuneCameraSensor()` rồi nạp lại. |
| Dashboard báo lỗi bảo mật / muốn bật xác thực LAN | Biến môi trường bảo mật | Có thể đặt `SMARTDOOR_API_KEY` và `SMARTDOOR_DEVICE_TOKEN` trước khi chạy server nếu dùng trên mạng Wi-Fi chung. |
