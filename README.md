# SmartDoor AIoT

Hệ thống khóa cửa thông minh AIoT gồm **ESP32-CAM** (thu hình ảnh QVGA stream qua WebSocket & điều khiển relay), **ESP32-S3** (tùy chọn — điều khiển servo, cảm biến HC-SR04/MC-38, loa MP3), và **FastAPI + ONNX Server** chạy 4 mô hình AI.

## 1. Cài đặt và chạy Server

1. Cài **Python 3.12 (64-bit)**, sau đó chạy `start_server.bat` (hoặc `smartdoor-server\run.bat`).
2. Script sẽ tự tạo/kích hoạt `smartdoor-server\.venv`, cài `requirements.txt`, in ra **IPv4 LAN** của máy tính và khởi động server tại `http://127.0.0.1:8000/`.
3. Mở Firewall Windows cho cổng **8000 (TCP)** trên mạng Private LAN nếu ESP32-CAM không kết nối được tới server.

## 2. Cấu hình Wi-Fi & Nạp firmware ESP32-CAM

1. Copy file `wifi_config.example.h` ở thư mục gốc thành `wifi_config.h` (file này đã được `.gitignore` để không lộ mật khẩu Wi-Fi).
2. Điền đúng `WIFI_SSID`, `WIFI_PASSWORD` (băng tần **2.4 GHz**) và `SERVER_IP` (địa chỉ IPv4 LAN máy tính vừa in ở bước 1).
3. Mở `firmware\esp32_cam_smartdoor\esp32_cam_smartdoor.ino` bằng **Arduino IDE 2**:
   - Chọn Board: **AI Thinker ESP32-CAM** (ESP32 Arduino core 3.x).
   - Bật **PSRAM: Enabled** trong menu Tools (firmware vẫn có chế độ fallback DRAM nếu board không có PSRAM).
   - Cài 2 thư viện: **WebSockets** (Markus Sattler) và **ArduinoJson** (v7).
4. Giữ `IO0` nối `GND` khi nạp; nạp xong tháo `IO0` khỏi `GND` và nhấn nút Reset.
5. Mở **Serial Monitor (115200 baud)** để kiểm tra luồng ảnh:
   - Khi hoạt động đúng, cứ mỗi 5 giây board sẽ in:
     `[STAT] WiFi=OK | WS=OK | Cam=OK | Sent=50 | Drop=0 | Last=7842 B | RSSI=-48`
   - Server tự động chuyển sang nguồn **ESP32-CAM** khi nhận kết nối `/ws/esp32`, đưa ảnh vào Model 1/2/3 xử lý và hiển thị khung nhận diện trực tiếp trên Web Dashboard.
   - Có thể mở nhanh `http://127.0.0.1:8000/api/camera/snapshot.jpg` để xem ảnh JPEG mới nhất nhận từ ESP32-CAM.

## 3. Đấu dây Relay 12V (khi dùng ESP32-CAM điều khiển chốt trực tiếp)

- ESP32-CAM `GPIO13` → `IN` relay; nối chung `GND`.
- Nguồn 12V (+) → `COM`; `NO` → cực (+) solenoid; cực (−) solenoid → nguồn 12V (−). Mắc diode dập xung ngược song song cuộn solenoid.
- Không cấp nguồn solenoid từ chân 5V/3.3V của ESP32. Đổi `RELAY_ON` (`HIGH`/`LOW`) tùy loại module relay.
- `GPIO4` (LED flash) nháy ngắn khi có cảnh báo giả mạo; `GPIO15` TX gửi `OPEN\n` / `ALARM\n` (không dùng đồng thời thẻ nhớ microSD).

## 4. Firmware ESP32-S3 (Tùy chọn cho mạch chốt Servo + Cảm biến)

- Nằm tại `sketch_nov9a\sketch_nov9a.ino` (cùng `sound_data.h`, `sound_data_wrong.h`), dùng chung `wifi_config.h` ở thư mục gốc và kết nối tới `ws://<SERVER_IP>:8000/ws/esp32-control`.
- Chân đấu nối: Servo `GPIO18`, HC-SR04 `TRIG=7 / ECHO=8` (qua cầu phân áp 1kΩ/2kΩ), MC-38 `GPIO20`, nút bấm trong `GPIO19`, LED `GPIO21`, Buzzer `GPIO38`, loa I2S MAX98357A `BCLK=4 / LRC=5 / DIN=6`, mic INMP441 `SCK=14 / WS=15 / SD=16`.

## 5. Vận hành & Bảo mật tùy chọn

- Chọn chế độ `FACE_ONLY` hoặc `FACE_AND_VOICE_2FA` trên thanh công cụ.
- Các thành viên có sẵn trong `face_db.npy` (`person-001`..`person-004`) được tự động đồng bộ vào danh sách thành viên. Tab **Đăng ký mới** cho phép chụp mẫu trực tiếp từ luồng ESP32-CAM đang hiển thị.
- Nếu chạy trên mạng Wi-Fi công cộng/dùng chung, có thể đặt biến môi trường `SMARTDOOR_API_KEY` và `SMARTDOOR_DEVICE_TOKEN` trước khi chạy server.
