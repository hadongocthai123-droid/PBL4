/*
  SmartDoor AIoT — Firmware cho AI-Thinker ESP32-CAM.
  Chức năng:
    - Chụp JPEG QVGA (320x240) và stream dạng binary qua WebSocket /ws/esp32 lên server để AI xử lý.
    - Gửi HEARTBEAT telemetry (RSSI, IP, trạng thái camera/relay, số frame đã gửi).
    - Nhận lệnh DOOR_COMMAND (UNLOCK / LOCK) điều khiển relay và ALERT (SPOOF_DETECTED) nháy LED.

  Thư viện cần cài trong Arduino IDE 2 (ESP32 Arduino core 3.x):
    - WebSockets (Markus Sattler)
    - ArduinoJson 7

  Trước khi nạp: copy file wifi_config.example.h -> wifi_config.h ở thư mục gốc dự án
  và điền WIFI_SSID, WIFI_PASSWORD, SERVER_IP (IPv4 LAN của máy chạy server).
*/
#include <Arduino.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include "esp_camera.h"

#if __has_include("../../wifi_config.h")
  #include "../../wifi_config.h"
#else
  #error "Chua tao wifi_config.h! Hay copy wifi_config.example.h thanh wifi_config.h o thu muc goc PBL4 va dien WIFI_SSID, WIFI_PASSWORD, SERVER_IP."
#endif

// ---------- Chân phần cứng SmartDoor (AI-Thinker ESP32-CAM) ----------
static constexpr int RELAY_PIN = 13;            // Chân IN module relay 12V (không dùng chung microSD).
static constexpr int RELAY_ON = HIGH;           // Đổi sang LOW nếu dùng module relay active-low.
static constexpr int RELAY_OFF = (RELAY_ON == HIGH) ? LOW : HIGH;
static constexpr int SERIAL1_TX_PIN = 15;       // TX UART tùy chọn gửi OPEN/ALARM sang mạch phụ.
static constexpr int STATUS_LED_PIN = 4;        // LED flash tích hợp; nháy khi nhận cảnh báo spoof.
static constexpr int STATUS_LED_ON = HIGH;

// ---------- Tham số vận hành ----------
static constexpr uint32_t DEFAULT_UNLOCK_MS = 5000;
static constexpr uint32_t FRAME_INTERVAL_MS = 100;   // ~10 FPS: cân bằng mượt mà & tránh nghẽn TCP/AI.
static constexpr uint32_t HEARTBEAT_MS = 3000;
static constexpr uint32_t WIFI_RETRY_MS = 8000;      // Đủ thời gian hoàn tất DHCP trước khi thử lại.
static constexpr uint32_t STATS_LOG_MS = 5000;
static constexpr size_t   MAX_FRAME_BYTES = 65536;   // Bảo vệ buffer WebSocket với khung QVGA.

// Bản đồ chân camera AI-Thinker ESP32-CAM
static camera_config_t cameraConfig = {};

void configureCamera() {
  cameraConfig.pin_pwdn = 32;
  cameraConfig.pin_reset = -1;
  cameraConfig.pin_xclk = 0;
  cameraConfig.pin_sccb_sda = 26;
  cameraConfig.pin_sccb_scl = 27;
  cameraConfig.pin_d7 = 35;
  cameraConfig.pin_d6 = 34;
  cameraConfig.pin_d5 = 39;
  cameraConfig.pin_d4 = 36;
  cameraConfig.pin_d3 = 21;
  cameraConfig.pin_d2 = 19;
  cameraConfig.pin_d1 = 18;
  cameraConfig.pin_d0 = 5;
  cameraConfig.pin_vsync = 25;
  cameraConfig.pin_href = 23;
  cameraConfig.pin_pclk = 22;
  cameraConfig.xclk_freq_hz = 20000000;
  cameraConfig.ledc_timer = LEDC_TIMER_0;
  cameraConfig.ledc_channel = LEDC_CHANNEL_0;
  cameraConfig.pixel_format = PIXFORMAT_JPEG;
  cameraConfig.frame_size = FRAMESIZE_QVGA;  // 320x240 khớp tốt với Model 1 (320x320).

  if (psramFound()) {
    cameraConfig.jpeg_quality = 12;          // Chất lượng cao hơn khi có PSRAM (số nhỏ = nét hơn).
    cameraConfig.fb_count = 2;
    cameraConfig.grab_mode = CAMERA_GRAB_LATEST;
    cameraConfig.fb_location = CAMERA_FB_IN_PSRAM;
  } else {
    cameraConfig.jpeg_quality = 16;          // Fallback an toàn nếu chưa bật PSRAM trong Arduino IDE.
    cameraConfig.fb_count = 1;
    cameraConfig.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
    cameraConfig.fb_location = CAMERA_FB_IN_DRAM;
  }
}

void tuneCameraSensor() {
  sensor_t *s = esp_camera_sensor_get();
  if (!s) return;
  // Tự động cân bằng sáng/màu để AI nhận diện khuôn mặt ổn định trong nhiều điều kiện ánh sáng.
  s->set_whitebal(s, 1);
  s->set_awb_gain(s, 1);
  s->set_exposure_ctrl(s, 1);
  s->set_aec2(s, 1);
  s->set_gain_ctrl(s, 1);
  s->set_bpc(s, 1);
  s->set_wpc(s, 1);
  s->set_lenc(s, 1);
  // Bỏ comment 2 dòng dưới nếu lắp camera bị ngược chiều:
  // s->set_vflip(s, 1);
  // s->set_hmirror(s, 1);
}

WebSocketsClient webSocket;
bool cameraReady = false;
bool socketStarted = false;
bool socketConnected = false;
bool wifiWasConnected = false;
bool relayActive = false;
bool ledActive = false;

uint32_t relayOffAt = 0;
uint32_t ledOffAt = 0;
uint32_t lastFrameAt = 0;
uint32_t lastHeartbeatAt = 0;
uint32_t lastWifiAttemptAt = 0;
uint32_t lastStatsLogAt = 0;

uint32_t framesSent = 0;
uint32_t framesDropped = 0;
size_t   lastFrameBytes = 0;

static bool timeReached(uint32_t now, uint32_t deadline) {
  return static_cast<int32_t>(now - deadline) >= 0;
}

void activateRelay(uint32_t durationMs = DEFAULT_UNLOCK_MS) {
  if (durationMs < 1000) durationMs = 1000;
  if (durationMs > 30000) durationMs = 30000;
  digitalWrite(RELAY_PIN, RELAY_ON);
  relayActive = true;
  relayOffAt = millis() + durationMs;
  Serial1.print("OPEN\n");
  Serial.printf("[DOOR] Unlock: relay ON trong %lu ms\n", static_cast<unsigned long>(durationMs));
}

void lockDoor() {
  digitalWrite(RELAY_PIN, RELAY_OFF);
  relayActive = false;
  Serial.println("[DOOR] Lock: relay OFF");
}

void triggerSpoofAlert() {
  Serial1.print("ALARM\n");
  digitalWrite(STATUS_LED_PIN, STATUS_LED_ON);
  ledActive = true;
  ledOffAt = millis() + 350;
  Serial.println("[ALERT] Nhan canh bao SPOOF_DETECTED");
}

void sendHeartbeat(uint32_t now, bool force = false) {
  if (!socketConnected) return;
  if (!force && now - lastHeartbeatAt < HEARTBEAT_MS) return;

  JsonDocument doc;
  doc["event"] = "HEARTBEAT";
  doc["device"] = "esp32-cam";
  doc["door"] = relayActive ? "unlocking" : "locked";
  doc["rssi"] = WiFi.RSSI();
  doc["ip"] = WiFi.localIP().toString();
  doc["camera_ready"] = cameraReady;
  doc["frames_sent"] = framesSent;

  String json;
  serializeJson(doc, json);
  webSocket.sendTXT(json);
  lastHeartbeatAt = now;
}

void onWebSocketEvent(WStype_t type, uint8_t *payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      socketConnected = true;
      Serial.printf("[WS] Da ket noi toi ws://%s:%u/ws/esp32\n", SERVER_IP, SERVER_PORT);
      sendHeartbeat(millis(), true);
      return;

    case WStype_DISCONNECTED:
      socketConnected = false;
      Serial.println("[WS] Mat ket noi SmartDoor server; dang tu ket noi lai...");
      return;

    case WStype_ERROR:
      socketConnected = false;
      Serial.println("[WS] Loi WebSocket!");
      return;

    case WStype_TEXT:
      break;

    default:
      return;
  }

  JsonDocument doc;
  DeserializationError error = deserializeJson(doc, payload, length);
  if (error) {
    Serial.printf("[WS] JSON khong hop le: %s\n", error.c_str());
    return;
  }

  const char *eventName = doc["event"] | "";
  if (strcmp(eventName, "DOOR_COMMAND") == 0) {
    const char *action = doc["action"] | "";
    if (strcmp(action, "UNLOCK") == 0) {
      uint32_t durationSec = doc["duration_sec"] | (DEFAULT_UNLOCK_MS / 1000);
      activateRelay(durationSec * 1000UL);
      sendHeartbeat(millis(), true);
    } else if (strcmp(action, "LOCK") == 0) {
      lockDoor();
      sendHeartbeat(millis(), true);
    }
  } else if (strcmp(eventName, "ALERT") == 0) {
    const char *alertType = doc["type"] | "";
    if (strcmp(alertType, "SPOOF_DETECTED") == 0) {
      triggerSpoofAlert();
    }
  }
}

void startSocket() {
  if (socketStarted || WiFi.status() != WL_CONNECTED) return;
  webSocket.onEvent(onWebSocketEvent);
  webSocket.begin(SERVER_IP, SERVER_PORT, "/ws/esp32");
  webSocket.setReconnectInterval(3000);
  webSocket.enableHeartbeat(15000, 3000, 2);
  socketStarted = true;
  Serial.printf("[WS] Dang mo ket noi toi ws://%s:%u/ws/esp32 ...\n", SERVER_IP, SERVER_PORT);
}

void sendCameraFrame(uint32_t now) {
  if (!cameraReady || !socketConnected || now - lastFrameAt < FRAME_INTERVAL_MS) return;

  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    framesDropped++;
    return;
  }

  bool sent = false;
  if (fb->format == PIXFORMAT_JPEG && fb->len > 0 && fb->len <= MAX_FRAME_BYTES) {
    sent = webSocket.sendBIN(fb->buf, fb->len);
    if (sent) {
      framesSent++;
      lastFrameBytes = fb->len;
    } else {
      framesDropped++;
    }
  } else {
    framesDropped++;
  }

  esp_camera_fb_return(fb);
  lastFrameAt = now;
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n===== SmartDoor AIoT — ESP32-CAM =====");

  Serial1.begin(115200, SERIAL_8N1, -1, SERIAL1_TX_PIN);
  pinMode(RELAY_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, RELAY_OFF);
  pinMode(STATUS_LED_PIN, OUTPUT);
  digitalWrite(STATUS_LED_PIN, !STATUS_LED_ON);

  configureCamera();
  esp_err_t err = esp_camera_init(&cameraConfig);
  cameraReady = (err == ESP_OK);
  if (!cameraReady) {
    Serial.printf("[CAM] Khoi tao that bai (err=0x%x). Kiem tra chon board AI-Thinker va cap camera!\n", err);
  } else {
    tuneCameraSensor();
    Serial.printf("[CAM] San sang: QVGA 320x240 JPEG (PSRAM: %s)\n", psramFound() ? "CO" : "KHONG");
  }

  // Đăng ký callback WebSocket ngay từ setup để không bao giờ bị bỏ sót event.
  webSocket.onEvent(onWebSocketEvent);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // Tắt modem sleep giúp stream JPEG qua WebSocket ổn định, giảm trễ.
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  lastWifiAttemptAt = millis();
  Serial.printf("[WiFi] Dang ket noi '%s' -> Server %s:%u\n", WIFI_SSID, SERVER_IP, SERVER_PORT);
}

void loop() {
  const uint32_t now = millis();

  if (WiFi.status() != WL_CONNECTED) {
    wifiWasConnected = false;
    socketConnected = false;
    if (socketStarted) {
      webSocket.disconnect();
      socketStarted = false;
    }
    if (now - lastWifiAttemptAt >= WIFI_RETRY_MS) {
      lastWifiAttemptAt = now;
      WiFi.disconnect(false);
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
      Serial.println("[WiFi] Dang thu ket noi lai...");
    }
  } else {
    if (!wifiWasConnected) {
      wifiWasConnected = true;
      Serial.printf("[WiFi] Da ket noi! IP: %s | RSSI: %d dBm\n",
                    WiFi.localIP().toString().c_str(), WiFi.RSSI());
    }
    startSocket();
  }

  if (socketStarted) {
    webSocket.loop();
  }

  if (relayActive && timeReached(now, relayOffAt)) {
    lockDoor();
    sendHeartbeat(now, true);
  }

  if (ledActive && timeReached(now, ledOffAt)) {
    digitalWrite(STATUS_LED_PIN, !STATUS_LED_ON);
    ledActive = false;
  }

  sendHeartbeat(now);
  sendCameraFrame(now);

  if (now - lastStatsLogAt >= STATS_LOG_MS) {
    lastStatsLogAt = now;
    Serial.printf("[STAT] WiFi=%s | WS=%s | Cam=%s | Sent=%lu | Drop=%lu | Last=%u B | RSSI=%d\n",
                  WiFi.status() == WL_CONNECTED ? "OK" : "DOWN",
                  socketConnected ? "OK" : "DOWN",
                  cameraReady ? "OK" : "ERR",
                  static_cast<unsigned long>(framesSent),
                  static_cast<unsigned long>(framesDropped),
                  static_cast<unsigned int>(lastFrameBytes),
                  WiFi.RSSI());
  }

  yield();
}
