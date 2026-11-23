// ============================================================
//   HỆ THỐNG KHÓA CỬA THÔNG MINH - ESP32-S3
//   Phiên bản: 2.2 (Đồng bộ ArduinoJson v7, Wi-Fi/WS ổn định)
//   Sơ đồ đấu dây khớp Bảng 1 & Bảng 2:
//     - Servo (SG90/MG90S):  GPIO 18
//     - HC-SR04 (Siêu âm):   TRIG=GPIO 7, ECHO=GPIO 8 (qua cầu phân áp 1k/2k)
//     - MC-38 (Cảm biến từ): GPIO 20 (INPUT_PULLUP, nối GND)
//     - Nút bấm bên trong:   GPIO 19 (INPUT_PULLUP, nối GND)
//     - Đèn LED trạng thái:  GPIO 21 (qua trở 220Ω)
//     - Active Buzzer:       GPIO 38
//     - Loa I2S MAX98357A:   BCLK=GPIO 4, LRC=GPIO 5, DIN=GPIO 6
//     - Micro I2S INMP441:   SCK=GPIO 14, WS=GPIO 15, SD=GPIO 16 (VDD 3V3, L/R GND)
// ============================================================

#include <Arduino.h>
#include <ESP32Servo.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include "AudioFileSourcePROGMEM.h"
#include "AudioGeneratorMP3.h"
#include "AudioOutputI2S.h"
#include "sound_data.h"       // Âm thanh khi mật khẩu ĐÚNG (đặt cùng thư mục sketch_nov9a/)
#include "sound_data_wrong.h" // Âm thanh khi mật khẩu SAI  (đặt cùng thư mục sketch_nov9a/)

#if __has_include("../wifi_config.h")
  #include "../wifi_config.h"
#else
  #error "Chua tao wifi_config.h! Hay copy wifi_config.example.h thanh wifi_config.h o thu muc goc PBL4."
#endif

// ============================================================
//   KHAI BÁO CHÂN (PIN DEFINITIONS)
// ============================================================
#define SERVO_PIN        18  // Dây cam/vàng của Servo
#define HC_TRIG_PIN      7   // Chân TRIG của HC-SR04
#define HC_ECHO_PIN      8   // Chân ECHO của HC-SR04 (qua cầu phân áp 1kΩ / 2kΩ)
#define BUTTON_PIN       19  // Nút bấm mở cửa từ bên TRONG
#define LED_PIN          21  // Đèn LED báo hiệu (qua trở 220Ω)
#define BUZZER_PIN       38  // Còi báo hiệu (Active Buzzer 2 dây)
#define MC38_PIN         20  // Cảm ứng từ MC-38 (kiểm tra cửa đóng/mở)

// I2S Loa phát MP3 (MAX98357A)
#define I2S_SPK_BCLK_PIN 4
#define I2S_SPK_LRC_PIN  5
#define I2S_SPK_DIN_PIN  6

// I2S Micro thu âm (INMP441 — VDD nối 3V3, L/R nối GND; dự phòng cho thu âm từ phần cứng)
#define I2S_MIC_SCK_PIN  14
#define I2S_MIC_WS_PIN   15
#define I2S_MIC_SD_PIN   16

// ============================================================
//   CẤU HÌNH HỆ THỐNG
// ============================================================
const String CORRECT_PASSWORD    = "1234"; // Mật khẩu — đổi tại đây
const int    PERSON_DISTANCE_CM  = 50;     // Khoảng cách phát hiện người (cm)
const int    PERSON_TIMEOUT_MS   = 3000;   // Thời gian mất phát hiện → tắt LED (ms)
const int    PASSWORD_TIMEOUT_MS = 15000;  // Timeout nhập mật khẩu (ms)
const int    DOOR_LOCK_DELAY_MS  = 10000;  // Thời gian chờ sau khi cửa đóng → mới khóa servo (ms)
const int    DOOR_OPEN_ALERT_MS  = 30000;  // Cảnh báo nếu cửa chưa đóng sau 30 giây (ms)
const int    MAX_WRONG_ATTEMPTS  = 3;      // Số lần sai tối đa trước khi khóa
const int    LOCKOUT_TIME_MS     = 30000;  // Thời gian khóa sau nhiều lần sai (ms)
const int    SERVO_LOCKED        = 180;    // Góc Servo khi khóa cửa
const int    SERVO_OPEN          = 90;     // Góc Servo khi mở cửa
const unsigned long WIFI_RETRY_MS = 8000;
const unsigned long HEARTBEAT_MS  = 3000;

// ============================================================
//   TRẠNG THÁI HỆ THỐNG
// ============================================================
enum DoorState {
  DOOR_LOCKED,
  DOOR_OPENING,
  DOOR_OPEN,
  DOOR_CLOSING
};

DoorState currentDoorState = DOOR_LOCKED;
WebSocketsClient smartDoorSocket;
bool socketStarted = false;
bool socketConnected = false;
volatile int pendingRemoteCommand = 0; // 1 = unlock, 2 = lock
unsigned long lastWifiAttempt = 0;
unsigned long lastHeartbeat = 0;

// Biến cảm biến người
unsigned long lastSeenTime = 0;
bool isPersonDetected = false;

// Biến cửa
unsigned long doorOpenTime = 0;
unsigned long doorClosedTime = 0;
bool doorAlertSent = false;
bool waitingToLock = false;

// Biến chống spam sai mật khẩu
int wrongAttempts = 0;
unsigned long lockoutStartTime = 0;
bool isLockedOut = false;

// Biến trạng thái cảm ứng từ
bool prevMC38State = false;

// ============================================================
//   ĐỐI TƯỢNG
// ============================================================
Servo myServo;
AudioGeneratorMP3      *mp3 = nullptr;
AudioFileSourcePROGMEM *file = nullptr;
AudioOutputI2S         *out = nullptr;

// ============================================================
//   KHAI BÁO HÀM
// ============================================================
void handleOutsideAccess();
void handleInsideButton();
void checkMC38Status();
void openDoor(const String &reason);
void lockServo();
void handleCorrectPassword();
void handleWrongPassword(const String &input);
void playMP3Message();
void playMP3Wrong();
int  getDistance();
void beepBuzzer(int ms);
void beepPattern(int times, int onMs, int offMs);
void printDoorStatus();
void handleSmartDoorNetwork();
void handleSmartDoorSocketEvent(WStype_t type, uint8_t *payload, size_t length);
void processRemoteCommand();
void sendHeartbeat(bool force = false);
void sendAlert(const char *alertType);
const char *doorStateName();

// ============================================================
//   SETUP
// ============================================================
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n========================================");
  Serial.println("   HỆ THỐNG KHÓA CỬA THÔNG MINH v2.2  ");
  Serial.println("========================================");

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  lastWifiAttempt = millis();
  smartDoorSocket.onEvent(handleSmartDoorSocketEvent);
  Serial.printf("[WiFi] Dang ket noi '%s' -> Server %s:%u\n", WIFI_SSID, SERVER_IP, SERVER_PORT);

  pinMode(HC_TRIG_PIN, OUTPUT);
  pinMode(HC_ECHO_PIN, INPUT);
  pinMode(BUTTON_PIN,  INPUT_PULLUP);
  pinMode(LED_PIN,     OUTPUT);
  pinMode(BUZZER_PIN,  OUTPUT);
  pinMode(MC38_PIN,    INPUT_PULLUP);

  digitalWrite(LED_PIN,    LOW);
  digitalWrite(BUZZER_PIN, LOW);

  ESP32PWM::allocateTimer(0);
  myServo.setPeriodHertz(50);
  myServo.attach(SERVO_PIN, 500, 2400);
  myServo.write(SERVO_LOCKED);
  delay(1000);

  prevMC38State = (digitalRead(MC38_PIN) == LOW);

  Serial.println("\n[SETUP] Trạng thái ban đầu:");
  Serial.println("  > Servo: KHÓA (180°)");
  printDoorStatus();
  Serial.println("========================================\n");

  beepBuzzer(200);
}

// ============================================================
//   LOOP CHÍNH
// ============================================================
void loop() {
  handleSmartDoorNetwork();
  processRemoteCommand();

  // ---- [1] KIỂM TRA KHÓA TẠM THỜI (Sau nhiều lần sai MK) ----
  if (isLockedOut) {
    if (millis() - lockoutStartTime >= LOCKOUT_TIME_MS) {
      isLockedOut   = false;
      wrongAttempts = 0;
      Serial.println("[HT] Hết thời gian khóa. Hệ thống sẵn sàng lại.\n");
      beepBuzzer(100);
    } else {
      unsigned long remaining = (LOCKOUT_TIME_MS - (millis() - lockoutStartTime)) / 1000;
      static unsigned long lastPrint = 0;
      if (millis() - lastPrint > 5000) {
        Serial.println("[HT] Hệ thống đang bị khóa! Còn " + String(remaining) + " giây...");
        lastPrint = millis();
      }
      delay(50);
      return;
    }
  }

  // ---- [2] CẢM BIẾN SIÊU ÂM: Phát hiện người bên ngoài ----
  int distance = getDistance();

  if (distance > 0 && distance < PERSON_DISTANCE_CM) {
    if (!isPersonDetected) {
      Serial.println("[SENSOR] Phát hiện người! (khoảng cách: " + String(distance) + " cm)");
      digitalWrite(LED_PIN, HIGH);
      beepBuzzer(50);
      isPersonDetected = true;
    }
    lastSeenTime = millis();
  } else if (isPersonDetected && (millis() - lastSeenTime > PERSON_TIMEOUT_MS)) {
    Serial.println("[SENSOR] Người đã rời đi. Tắt đèn LED.");
    digitalWrite(LED_PIN, LOW);
    isPersonDetected = false;
  }

  // ---- [3] NÚT BẤM TỪ BÊN TRONG: Mở/Khóa ngay ----
  if (digitalRead(BUTTON_PIN) == LOW) {
    delay(50);
    if (digitalRead(BUTTON_PIN) == LOW) {
      handleInsideButton();
      while (digitalRead(BUTTON_PIN) == LOW) {
        if (socketStarted) smartDoorSocket.loop();
        delay(10);
      }
    }
  }

  // ---- [4] NHẬP MẬT KHẨU QUA SERIAL KHI CÓ NGƯỜI ----
  if (isPersonDetected && currentDoorState == DOOR_LOCKED) {
    if (Serial.available()) {
      handleOutsideAccess();
    }
  }

  // ---- [5] THEO DÕI CẢM ỨNG TỪ MC-38 ----
  checkMC38Status();

  // ---- [6] ĐẾM 10S SAU KHI CỬA ĐÓNG → KHÓA SERVO ----
  if (waitingToLock) {
    unsigned long elapsed = millis() - doorClosedTime;
    unsigned long remaining = (elapsed < DOOR_LOCK_DELAY_MS) ? ((DOOR_LOCK_DELAY_MS - elapsed) / 1000) : 0;

    static unsigned long lastCountPrint = 0;
    if (millis() - lastCountPrint >= 1000) {
      Serial.println("[CỬA] Khóa servo sau: " + String(remaining + 1) + " giây...");
      lastCountPrint = millis();
    }

    bool doorStillClosed = (digitalRead(MC38_PIN) == LOW);
    if (!doorStillClosed) {
      Serial.println("[CỬA] Cửa bị mở lại — hủy đếm ngược, chờ cửa đóng lại.");
      waitingToLock = false;
    } else if (elapsed >= DOOR_LOCK_DELAY_MS) {
      waitingToLock = false;
      lockServo();
    }
  }

  // ---- [7] CẢNH BÁO CỬA CHƯA ĐÓNG SAU 30S ----
  if (currentDoorState == DOOR_OPEN && !waitingToLock && !doorAlertSent) {
    if (millis() - doorOpenTime > DOOR_OPEN_ALERT_MS) {
      Serial.println("[CỬA] ⚠ Cảnh báo: Cửa vẫn chưa được đóng sau " + String(DOOR_OPEN_ALERT_MS / 1000) + " giây!");
      beepPattern(3, 300, 150);
      doorAlertSent = true;
    }
  }

  delay(20);
}

const char *doorStateName() {
  return digitalRead(MC38_PIN) == LOW ? "closed" : "open";
}

void sendHeartbeat(bool force) {
  const unsigned long now = millis();
  if (!socketConnected) return;
  if (!force && now - lastHeartbeat < HEARTBEAT_MS) return;

  lastHeartbeat = now;
  JsonDocument doc;
  doc["event"] = "HEARTBEAT";
  doc["device"] = "esp32-s3";
  doc["door"] = doorStateName();
  doc["locked"] = (currentDoorState == DOOR_LOCKED);
  doc["rssi"] = WiFi.RSSI();

  String payload;
  serializeJson(doc, payload);
  smartDoorSocket.sendTXT(payload);
}

void sendAlert(const char *alertType) {
  if (!socketConnected) return;
  JsonDocument doc;
  doc["event"] = "ALERT";
  doc["device"] = "esp32-s3";
  doc["type"] = alertType;
  String payload;
  serializeJson(doc, payload);
  smartDoorSocket.sendTXT(payload);
}

void handleSmartDoorNetwork() {
  const unsigned long now = millis();
  if (WiFi.status() != WL_CONNECTED) {
    socketConnected = false;
    if (socketStarted) {
      smartDoorSocket.disconnect();
      socketStarted = false;
    }
    if (now - lastWifiAttempt >= WIFI_RETRY_MS) {
      lastWifiAttempt = now;
      WiFi.disconnect(false);
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
      Serial.println("[WiFi] Dang thu ket noi lai...");
    }
    return;
  }

  if (!socketStarted) {
    smartDoorSocket.begin(SERVER_IP, SERVER_PORT, "/ws/esp32-control");
    smartDoorSocket.setReconnectInterval(3000);
    smartDoorSocket.enableHeartbeat(15000, 3000, 2);
    socketStarted = true;
    Serial.printf("[SmartDoor] Dang ket noi ws://%s:%u/ws/esp32-control ...\n", SERVER_IP, SERVER_PORT);
  }

  smartDoorSocket.loop();
  sendHeartbeat(false);
}

void handleSmartDoorSocketEvent(WStype_t type, uint8_t *payload, size_t length) {
  if (type == WStype_CONNECTED) {
    socketConnected = true;
    Serial.println("[SmartDoor] Da ket noi WebSocket.");
    sendHeartbeat(true);
  } else if (type == WStype_DISCONNECTED) {
    socketConnected = false;
    Serial.println("[SmartDoor] Mat WebSocket, se tu ket noi lai.");
  } else if (type == WStype_TEXT) {
    JsonDocument doc;
    if (deserializeJson(doc, payload, length)) return;
    const char *event = doc["event"] | "";
    const char *action = doc["action"] | "";
    if (strcmp(event, "DOOR_COMMAND") == 0) {
      if (strcmp(action, "UNLOCK") == 0) pendingRemoteCommand = 1;
      else if (strcmp(action, "LOCK") == 0) pendingRemoteCommand = 2;
    }
  }
}

void processRemoteCommand() {
  int command = pendingRemoteCommand;
  if (!command) return;
  pendingRemoteCommand = 0;

  if (command == 1) {
    if (!isLockedOut && currentDoorState == DOOR_LOCKED) {
      openDoor("Lenh SmartDoor");
    } else {
      Serial.println("[SmartDoor] Tu choi mo: he thong dang khoa tam hoac cua khong o trang thai khoa.");
    }
  } else if (command == 2) {
    if (digitalRead(MC38_PIN) == LOW) {
      waitingToLock = false;
      if (currentDoorState != DOOR_LOCKED) lockServo();
    } else {
      Serial.println("[SmartDoor] Tu choi khoa vi cua chua dong.");
      beepPattern(2, 100, 100);
    }
  }
  sendHeartbeat(true);
}

void handleInsideButton() {
  Serial.println("\n[NÚT TRONG] Nút bấm bên trong được nhấn!");
  beepBuzzer(80);

  if (currentDoorState == DOOR_LOCKED) {
    openDoor("Nút bấm bên trong");
    sendHeartbeat(true);
    return;
  }

  if (currentDoorState == DOOR_OPEN || waitingToLock) {
    bool doorPhysicallyClosed = (digitalRead(MC38_PIN) == LOW);
    if (doorPhysicallyClosed) {
      waitingToLock = false;
      lockServo();
      sendHeartbeat(true);
    } else {
      Serial.println("[NÚT TRONG] ⚠ Cửa vật lý chưa đóng hẳn, không thể khóa.");
      beepPattern(2, 200, 150);
    }
    return;
  }

  Serial.println("[NÚT TRONG] Hệ thống đang xử lý — bỏ qua.");
}

void handleOutsideAccess() {
  Serial.println("\n[NGOÀI] Nhập mật khẩu rồi nhấn Enter (timeout 15s):");
  beepBuzzer(100);

  while (Serial.available()) Serial.read();

  unsigned long startWait = millis();
  String inputPassword = "";
  bool received = false;

  while (millis() - startWait < PASSWORD_TIMEOUT_MS) {
    if (socketStarted) smartDoorSocket.loop();
    if (Serial.available()) {
      inputPassword = Serial.readStringUntil('\n');
      inputPassword.trim();
      received = true;
      break;
    }
    delay(10);
  }

  if (!received) {
    Serial.println("[NGOÀI] Hết thời gian nhập mật khẩu!");
    beepPattern(2, 100, 200);
  } else if (inputPassword == CORRECT_PASSWORD) {
    wrongAttempts = 0;
    handleCorrectPassword();
  } else {
    handleWrongPassword(inputPassword);
  }
}

void handleCorrectPassword() {
  Serial.println("\n[✔] MẬT KHẨU ĐÚNG! Bắt đầu mở cửa...");
  beepBuzzer(500);
  openDoor("Mật khẩu đúng từ bên ngoài");
  sendHeartbeat(true);
}

void handleWrongPassword(const String &input) {
  wrongAttempts++;
  Serial.println("\n[✘] MẬT KHẨU SAI! (Nhập: \"" + input + "\")");
  Serial.println("[✘] Số lần sai: " + String(wrongAttempts) + "/" + String(MAX_WRONG_ATTEMPTS));

  playMP3Wrong();
  beepPattern(5, 150, 150);

  for (int i = 0; i < 3; i++) {
    beepBuzzer(300);
    delay(200);
  }

  digitalWrite(LED_PIN, LOW);

  if (wrongAttempts >= MAX_WRONG_ATTEMPTS) {
    isLockedOut      = true;
    lockoutStartTime = millis();
    Serial.println("[✘] KHÓA HỆ THỐNG " + String(LOCKOUT_TIME_MS / 1000) + " giây do sai quá nhiều lần!");
    for (int i = 0; i < 5; i++) {
      beepBuzzer(500);
      delay(100);
    }
  }
}

void openDoor(const String &reason) {
  Serial.println("[CỬA] Mở cửa — Lý do: " + reason);
  currentDoorState = DOOR_OPENING;

  digitalWrite(LED_PIN, HIGH);
  myServo.write(SERVO_OPEN);
  delay(600);

  currentDoorState = DOOR_OPEN;
  doorOpenTime     = millis();
  doorAlertSent    = false;
  waitingToLock    = false;

  if (reason.indexOf("Mật khẩu") >= 0) {
    playMP3Message();
  }
}

void lockServo() {
  Serial.println("\n[CỬA] Khóa servo → " + String(SERVO_LOCKED) + "°");
  myServo.write(SERVO_LOCKED);
  delay(600);

  digitalWrite(LED_PIN, LOW);
  currentDoorState = DOOR_LOCKED;

  beepBuzzer(150);
  Serial.println("[CỬA] ✔ Cửa đã KHÓA hoàn toàn.\n");
}

void checkMC38Status() {
  bool doorClosed = (digitalRead(MC38_PIN) == LOW);

  if (doorClosed != prevMC38State) {
    if (doorClosed) {
      Serial.println("[MC-38] Cửa vừa ĐÓNG.");
      if (currentDoorState == DOOR_OPEN && !waitingToLock) {
        doorClosedTime = millis();
        waitingToLock  = true;
        doorAlertSent  = true;
        Serial.println("[MC-38] ✔ Bắt đầu đếm 10 giây → sẽ khóa servo.");
      }
    } else {
      Serial.println("[MC-38] Cửa vừa MỞ.");
      if (currentDoorState == DOOR_LOCKED) {
        Serial.println("[MC-38] ⚠⚠ CẢNH BÁO: Cửa bị mở khi ĐANG KHÓA!");
        sendAlert("INTRUSION");
        for (int i = 0; i < 10; i++) {
          beepBuzzer(200);
          if (socketStarted) smartDoorSocket.loop();
          delay(100);
        }
      }
    }
    prevMC38State = doorClosed;
    sendHeartbeat(true);
  }
}

void printDoorStatus() {
  bool doorClosed = (digitalRead(MC38_PIN) == LOW);
  Serial.print("  > MC-38 (Cảm ứng từ): Cửa đang ");
  Serial.println(doorClosed ? "ĐÓNG ✔" : "MỞ ⚠");
}

void playMP3Message() {
  Serial.println("[AUDIO] Đang phát MP3 chào mừng...");
  out  = new AudioOutputI2S();
  out->SetPinout(I2S_SPK_BCLK_PIN, I2S_SPK_LRC_PIN, I2S_SPK_DIN_PIN);
  out->SetGain(1.5);

  file = new AudioFileSourcePROGMEM(mp3_data, sizeof(mp3_data));
  mp3  = new AudioGeneratorMP3();
  mp3->begin(file, out);

  while (mp3->isRunning()) {
    if (socketStarted) smartDoorSocket.loop();
    if (!mp3->loop()) {
      mp3->stop();
      break;
    }
    yield();
  }

  delete mp3;  mp3 = nullptr;
  delete file; file = nullptr;
  delete out;  out = nullptr;
  Serial.println("[AUDIO] Phát MP3 xong.");
}

void playMP3Wrong() {
  Serial.println("[AUDIO] Đang phát MP3 cảnh báo sai mật khẩu...");
  out  = new AudioOutputI2S();
  out->SetPinout(I2S_SPK_BCLK_PIN, I2S_SPK_LRC_PIN, I2S_SPK_DIN_PIN);
  out->SetGain(1.5);

  file = new AudioFileSourcePROGMEM(mp3_data_wrong, sizeof(mp3_data_wrong));
  mp3  = new AudioGeneratorMP3();
  mp3->begin(file, out);

  while (mp3->isRunning()) {
    if (socketStarted) smartDoorSocket.loop();
    if (!mp3->loop()) {
      mp3->stop();
      break;
    }
    yield();
  }

  delete mp3;  mp3 = nullptr;
  delete file; file = nullptr;
  delete out;  out = nullptr;
  Serial.println("[AUDIO] Phát MP3 cảnh báo xong.");
}

int getDistance() {
  digitalWrite(HC_TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(HC_TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(HC_TRIG_PIN, LOW);

  long duration = pulseIn(HC_ECHO_PIN, HIGH, 20000);
  if (duration == 0) return 999;
  return static_cast<int>(duration * 0.034 / 2);
}

void beepBuzzer(int ms) {
  digitalWrite(BUZZER_PIN, HIGH);
  delay(ms);
  digitalWrite(BUZZER_PIN, LOW);
}

void beepPattern(int times, int onMs, int offMs) {
  for (int i = 0; i < times; i++) {
    digitalWrite(BUZZER_PIN, HIGH);
    digitalWrite(LED_PIN,    HIGH);
    delay(onMs);
    digitalWrite(BUZZER_PIN, LOW);
    digitalWrite(LED_PIN,    LOW);
    if (i < times - 1) delay(offMs);
  }
}
