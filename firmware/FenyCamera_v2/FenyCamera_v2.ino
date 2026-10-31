#include <Arduino.h>
#include "esp_camera.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include "mbedtls/base64.h"

// ===========================
// Feny (office-01) firmware v2 - Oct 30, 2026
// Stock CameraWebServer base + mabel-world reporting:
//   - every OBS_INTERVAL_MS: light observation JSON -> mabel-gw listener
//   - every STILL_INTERVAL_MS: VGA still, base64-in-JSON -> same listener
//   - /capture and the stock web server stay live for debugging + flip tests
// WiFi creds are PLACEHOLDERS in this file (repo-safe). The real values are
// transplanted into the device copy at install time and never committed.
// ===========================

// ===========================
// Select camera model in board_config.h
// ===========================
#include "board_config.h"

// ===========================
// Enter your WiFi credentials (transplanted at install; keep placeholders here)
// ===========================
const char *ssid = "__WIFI_SSID__";
const char *password = "__WIFI_PASSWORD__";

// ===========================
// mabel-world reporting config
// ===========================
#define NODE_NAME "office-01"
#define GW_HOST "__GW_HOST__"   // your gateway host (the machine running gateway/listener.py)
#define GW_PORT 8891               // mabel-world listener
#define OBS_INTERVAL_MS 10000UL
#define STILL_INTERVAL_MS 60000UL  // 60s for tonight's proof; raise to 300000 later
#define FENY_FLIPPED 0             // 1 when the housing mounts her rotated 180 deg
#define FENY_ALS 1                 // LTR-308 ambient light sensor on the SCCB bus

// LTR-308 ALS: I2C 0x53, shares the camera SCCB bus (SIOD/SIOC)
#define ALS_ADDR 0x53
bool alsOk = false;

void startCameraServer();
void setupLedFlash();

unsigned long lastObs = 0;
unsigned long lastStill = 0;

bool alsInit() {
  Wire.begin(SIOD_GPIO_NUM, SIOC_GPIO_NUM);
  Wire.beginTransmission(ALS_ADDR);
  if (Wire.endTransmission() != 0) return false;
  // MAIN_CTRL (0x00): ALS enable = 0x02
  Wire.beginTransmission(ALS_ADDR);
  Wire.write(0x00); Wire.write(0x02);
  if (Wire.endTransmission() != 0) return false;
  // ALS_GAIN (0x05): 0x00 = 1x
  Wire.beginTransmission(ALS_ADDR);
  Wire.write(0x05); Wire.write(0x00);
  if (Wire.endTransmission() != 0) return false;
  return true;
}

long alsRead() {
  // ALS data: 0x0D (LSB), 0x0E, 0x0F (MSB, low nibble) -> 20-bit raw counts
  Wire.beginTransmission(ALS_ADDR);
  Wire.write(0x0D);
  if (Wire.endTransmission(false) != 0) return -1;
  if (Wire.requestFrom(ALS_ADDR, 3) != 3) return -1;
  long lo = Wire.read();
  long mid = Wire.read();
  long hi = Wire.read();
  return ((hi & 0x0F) << 16) | (mid << 8) | lo;
}

bool postJSON(const String &payload) {
  HTTPClient http;
  http.setTimeout(3000);
  String url = String("http://") + GW_HOST + ":" + GW_PORT + "/";
  if (!http.begin(url)) return false;
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(payload);
  http.end();
  return code == 200;
}

void postObservation() {
  long als = -1;
#if FENY_ALS
  if (alsOk) als = alsRead();
#endif
  String payload = String("{\"node\":\"") + NODE_NAME +
                   "\",\"type\":\"observation\",\"uptime_s\":" + String(millis() / 1000) +
                   ",\"ip\":\"" + WiFi.localIP().toString() +
                   "\",\"als_raw\":" + String(als) + "}";
  bool ok = postJSON(payload);
  Serial.printf("[obs] als=%ld post=%s\n", als, ok ? "ok" : "FAIL");
}

void postStill() {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) { Serial.println("[still] capture FAILED"); return; }
  size_t jpgLen = fb->len;
  int w = fb->width, h = fb->height;

  // Streamed hand-rolled POST: header, JSON prefix, base64 in 3KB->4KB chunks, suffix.
  size_t b64Len = 4 * ((jpgLen + 2) / 3);
  String prefix = String("{\"node\":\"") + NODE_NAME +
                  "\",\"type\":\"still\",\"uptime_s\":" + String(millis() / 1000) +
                  ",\"ip\":\"" + WiFi.localIP().toString() +
                  "\",\"w\":" + String(w) + ",\"h\":" + String(h) +
                  ",\"format\":\"jpeg\",\"jpeg_b64\":\"";
  String suffix = "\"}";
  size_t total = prefix.length() + b64Len + suffix.length();

  WiFiClient client;
  client.setTimeout(5);
  if (!client.connect(GW_HOST, GW_PORT)) {
    Serial.println("[still] connect FAILED");
    esp_camera_fb_return(fb);
    return;
  }
  client.printf("POST / HTTP/1.1\r\nHost: %s:%d\r\nContent-Type: application/json\r\nContent-Length: %u\r\nConnection: close\r\n\r\n",
                GW_HOST, GW_PORT, (unsigned)total);
  client.print(prefix);
  const size_t CH = 3000; // multiple of 3 -> clean base64 chunk boundaries
  unsigned char enc[4004];
  for (size_t off = 0; off < jpgLen; off += CH) {
    size_t n = min(CH, jpgLen - off);
    size_t outLen = 0;
    mbedtls_base64_encode(enc, sizeof(enc), &outLen, fb->buf + off, n);
    client.write(enc, outLen);
  }
  client.print(suffix);
  esp_camera_fb_return(fb);

  unsigned long t0 = millis();
  while (!client.available() && millis() - t0 < 3000) delay(10);
  int code = -1;
  if (client.available()) {
    String line = client.readStringUntil('\n'); // "HTTP/1.1 200 OK"
    int sp = line.indexOf(' ');
    if (sp > 0) code = line.substring(sp + 1, sp + 4).toInt();
  }
  client.stop();
  Serial.printf("[still] %dx%d %uB b64 -> http %d\n", w, h, (unsigned)jpgLen, code);
}

void setup() {
  Serial.begin(115200);
  Serial.setDebugOutput(true);
  Serial.println();

  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.frame_size = FRAMESIZE_VGA;   // v2: VGA working size (stills + stream)
  config.pixel_format = PIXFORMAT_JPEG;  // for streaming
  config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.jpeg_quality = 12;
  config.fb_count = 1;

  if (config.pixel_format == PIXFORMAT_JPEG) {
    if (psramFound()) {
      config.jpeg_quality = 10;
      config.fb_count = 2;
      config.grab_mode = CAMERA_GRAB_LATEST;
    } else {
      config.frame_size = FRAMESIZE_SVGA;
      config.fb_location = CAMERA_FB_IN_DRAM;
    }
  }

  // camera init
  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("Camera init failed with error 0x%x", err);
    return;
  }

  sensor_t *s = esp_camera_sensor_get();
  if (s->id.PID == OV3660_PID) {
#if FENY_FLIPPED
    // Board physically rotated 180 deg in the housing: toggle both axes
    // relative to the upright correction below.
    s->set_vflip(s, 0);
    s->set_hmirror(s, 1);
#else
    s->set_vflip(s, 1);        // flip it back
#endif
    s->set_brightness(s, 1);   // up the brightness just a bit
    s->set_saturation(s, -2);  // lower the saturation
  }

// Setup LED FLash if LED pin is defined in camera_pins.h
#if defined(LED_GPIO_NUM)
  setupLedFlash();
#endif

  WiFi.begin(ssid, password);
  WiFi.setSleep(false);

  Serial.print("WiFi connecting");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("");
  Serial.println("WiFi connected");

  startCameraServer();

#if FENY_ALS
  alsOk = alsInit();
  Serial.printf("ALS (LTR-308): %s\n", alsOk ? "ok" : "not found - light posts carry als_raw:-1");
#endif

  Serial.print("Camera Ready! Use 'http://");
  Serial.print(WiFi.localIP());
  Serial.println("' to connect");
  Serial.printf("Reporting to mabel-gw listener http://%s:%d/ as node %s\n", GW_HOST, GW_PORT, NODE_NAME);

  // First reports go out right away so the proof doesn't wait a full interval.
  postObservation();
  postStill();
  lastObs = millis();
  lastStill = millis();
}

void loop() {
  unsigned long now = millis();
  if (now - lastObs >= OBS_INTERVAL_MS) {
    lastObs = now;
    postObservation();
  }
  if (now - lastStill >= STILL_INTERVAL_MS) {
    lastStill = now;
    postStill();
  }
  delay(50);
}
