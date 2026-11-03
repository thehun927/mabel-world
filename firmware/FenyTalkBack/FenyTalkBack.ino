// FenyTalkBack — the round trip, on the node.
//
// Press BOOT (or send 'a' over serial) and Fény:
//   1. records REC_SECONDS of audio from her PDM microphone,
//   2. POSTs the WAV to the feny-loop endpoint on the gateway
//      (STT -> reply -> her house voice, rendered on KING),
//   3. plays the reply WAV through her own MAX98357 + speaker.
//
// One I2S peripheral, reconfigured between RX (PDM) and TX (STD).
// Mic recipe follows the field-proven reference for this board:
// pinMode(41, OUTPUT) + setPinsPdmRx(38, 39) + recordWAV(). The manual
// readBytes pattern yields a constant DC pattern on this board — the
// library helper does whatever the peripheral actually needs.
//
// Wi-Fi credentials live in the Mac-local feny_secrets.h beside this
// sketch (never committed).

#include <ESP_I2S.h>
#include <HTTPClient.h>
#include <WiFi.h>

#include "feny_secrets.h"

#define ASK_URL "http://GW_HOST_PLACEHOLDER:8892/ask"
#define BOOT_PIN 0
#define LED_PIN 3
#define REC_RATE 16000
#define REC_SECONDS 5
#define PLAY_RATE 24000
#define MIC_CLK 38
#define MIC_DATA 39
#define SPK_BCLK 45
#define SPK_LRC 46
#define SPK_DOUT 42
#define GAIN_NUM 19
#define GAIN_DEN 10

I2SClass i2s;     // microphone (PDM RX)
I2SClass i2sSpk;  // speaker (STD TX) — separate instance, like the reference;
                  // reconfiguring one instance record->play corrupts state

static void led(bool on) { digitalWrite(LED_PIN, on ? HIGH : LOW); }

static void wifiUp() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.print("[talk] WiFi connecting");
  WiFi.mode(WIFI_STA);
  WiFi.begin(FENY_WIFI_SSID, FENY_WIFI_PASS);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) {
    delay(300);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    WiFi.setSleep(false);  // v2's lesson: modem sleep murders round trips
    Serial.printf("[talk] WiFi up: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("[talk] WiFi FAILED");
  }
}

// Record one question. Returns a recordWAV() buffer (caller frees) and its
// size via outSize, or nullptr on failure. The PCM is peak-normalized in
// place: this microphone runs quiet (a spoken question peaks ~15-20% FS),
// and whisper hears a normalized signal far better.
static uint8_t *recordAsk(size_t *outSize) {
  i2s.setPinsPdmRx(MIC_CLK, MIC_DATA);
  if (!i2s.begin(I2S_MODE_PDM_RX, REC_RATE, I2S_DATA_BIT_WIDTH_16BIT,
                 I2S_SLOT_MODE_MONO)) {
    Serial.println("[talk] PDM RX begin FAILED");
    return nullptr;
  }
  Serial.printf("[talk] listening for %d s...\n", REC_SECONDS);
  led(true);
  size_t wavSize = 0;
  uint8_t *wav = i2s.recordWAV(REC_SECONDS, &wavSize);
  led(false);
  // Do NOT i2s.end() here: after recordWAV it asserts in heap_caps_free
  // ("free() target pointer is outside heap areas") and reboots the board —
  // found the hard way, Nov 1. The next begin() de-inits the peripheral
  // itself, and probe builds ran end()-free without issue.
  if (!wav || wavSize <= 44) {
    Serial.println("[talk] recordWAV FAILED");
    return nullptr;
  }
  int16_t *pcm = (int16_t *)(wav + 44);
  size_t n = (wavSize - 44) / 2;
  int32_t peak = 0;
  for (size_t i = 0; i < n; i++) {
    int32_t v = pcm[i] < 0 ? -(int32_t)pcm[i] : pcm[i];
    if (v > peak) peak = v;
  }
  Serial.printf("[talk] mic peak: %d (%.1f%% FS)\n", (int)peak,
                100.0 * (double)peak / 32768.0);
  if (peak > 0 && peak < 27853) {
    double g = 27853.0 / (double)peak;
    if (g > 12.0) g = 12.0;
    for (size_t i = 0; i < n; i++) {
      int32_t v = (int32_t)(pcm[i] * g);
      if (v > 32767) v = 32767;
      if (v < -32768) v = -32768;
      pcm[i] = (int16_t)v;
    }
    Serial.printf("[talk] normalized x%.1f\n", g);
  }
  *outSize = wavSize;
  return wav;
}

// Parse a canonical PCM WAV (as served by the endpoint) and play it.
static void playWav(uint8_t *buf, size_t len) {
  if (len < 44 || memcmp(buf, "RIFF", 4) != 0) {
    Serial.println("[talk] reply is not a WAV");
    return;
  }
  uint16_t fmt = 0, ch = 0, bits = 0;
  uint32_t rate = 0;
  size_t pos = 12, dataOff = 0;
  uint32_t dataLen = 0;
  while (pos + 8 <= len) {
    uint32_t csize;
    memcpy(&csize, buf + pos + 4, 4);
    if (memcmp(buf + pos, "fmt ", 4) == 0) {
      memcpy(&fmt, buf + pos + 8, 2);
      memcpy(&ch, buf + pos + 10, 2);
      memcpy(&rate, buf + pos + 12, 4);
      memcpy(&bits, buf + pos + 22, 2);
    } else if (memcmp(buf + pos, "data", 4) == 0) {
      dataOff = pos + 8;
      dataLen = csize;
      break;
    }
    pos += 8 + csize + (csize & 1);
  }
  if (!dataOff || fmt != 1 || bits != 16) {
    Serial.printf("[talk] unsupported WAV fmt=%u bits=%u\n", fmt, bits);
    return;
  }
  if (ch == 0 || ch > 2) ch = 1;
  if (rate == 0) rate = PLAY_RATE;
  size_t frames = dataLen / (2 * ch);
  Serial.printf("[talk] speaking: %u Hz, %u ch, %u frames\n", (unsigned)rate,
                (unsigned)ch, (unsigned)frames);

  i2sSpk.setPins(SPK_BCLK, SPK_LRC, SPK_DOUT, -1, -1);
  if (!i2sSpk.begin(I2S_MODE_STD, rate, I2S_DATA_BIT_WIDTH_16BIT,
                    I2S_SLOT_MODE_STEREO)) {
    Serial.println("[talk] STD TX begin FAILED");
    return;
  }
  led(true);
  const size_t CH = 512;
  static int16_t out[CH * 2];
  const int16_t *in = (const int16_t *)(buf + dataOff);
  for (size_t f = 0; f < frames; f += CH) {
    size_t n = frames - f;
    if (n > CH) n = CH;
    for (size_t i = 0; i < n; i++) {
      int32_t v = in[(f + i) * ch];
      if (ch == 2) v = (v + in[(f + i) * ch + 1]) / 2;
      v = v * GAIN_NUM / GAIN_DEN;
      if (v > 32767) v = 32767;
      if (v < -32768) v = -32768;
      out[i * 2] = (int16_t)v;
      out[i * 2 + 1] = (int16_t)v;
    }
    i2sSpk.write((uint8_t *)out, n * 4);
  }
  i2sSpk.end();
  led(false);
  Serial.println("[talk] done speaking");
}

static void askOnce() {
  wifiUp();
  if (WiFi.status() != WL_CONNECTED) return;
  size_t wavLen = 0;
  uint8_t *wav = recordAsk(&wavLen);
  if (!wav) return;

  Serial.printf("[talk] asking the gateway (%u bytes)... rssi=%d\n",
                (unsigned)wavLen, (int)WiFi.RSSI());
  led(true);
  int code = -999;
  HTTPClient http;
  for (int attempt = 1; attempt <= 3 && code != 200; attempt++) {
    http.begin(ASK_URL);
    http.setTimeout(90000);
    http.addHeader("Content-Type", "audio/wav");
    code = http.POST(wav, wavLen);
    Serial.printf("[talk] POST attempt %d -> HTTP %d\n", attempt, code);
    if (code != 200) {
      http.end();
      delay(1200);
    }
  }
  free(wav);
  if (code != 200) {
    led(false);
    return;
  }
  int total = http.getSize();
  Serial.printf("[talk] gateway reply: %d bytes\n", total);
  WiFiClient *stream = http.getStreamPtr();
  size_t cap = total > 0 ? (size_t)total : (size_t)2 * 1024 * 1024;
  uint8_t *reply = (uint8_t *)ps_malloc(cap);
  if (!reply) {
    Serial.println("[talk] no PSRAM for reply");
    http.end();
    led(false);
    return;
  }
  size_t got = 0;
  unsigned long t0 = millis();
  while (millis() - t0 < 90000) {
    if (total > 0 && got >= (size_t)total) break;
    size_t avail = stream->available();
    if (avail) {
      size_t want = cap - got;
      if (avail < want) want = avail;
      int n = stream->readBytes(reply + got, want);
      if (n > 0) got += n;
    } else {
      if (!http.connected() && (total < 0 || got >= (size_t)total)) break;
      delay(5);
    }
  }
  http.end();
  led(false);
  Serial.printf("[talk] reply buffered: %u bytes\n", (unsigned)got);
  if (got >= 44) playWav(reply, got);
  free(reply);
}

void setup() {
  Serial.begin(115200);
  delay(300);
  pinMode(BOOT_PIN, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  pinMode(41, OUTPUT);  // per the reference recipe for this board's audio
  led(false);
  Serial.println();
  Serial.println("[talk] FenyTalkBack v3 (dual-I2S, no end after recordWAV) — BOOT or 'a' to ask");
  wifiUp();
}

void loop() {
  wifiUp();
  static int lastBoot = HIGH;
  int b = digitalRead(BOOT_PIN);
  if (lastBoot == HIGH && b == LOW) {
    delay(30);
    if (digitalRead(BOOT_PIN) == LOW) askOnce();
  }
  lastBoot = b;
  if (Serial.available()) {
    char c = Serial.read();
    if (c == 'a' || c == 'A') askOnce();
    if (c == 'g' || c == 'G' || c == 'h' || c == 'H' || c == 'i' || c == 'I') {
      const char *url = c == 'g' || c == 'G'   ? "http://GW_HOST_PLACEHOLDER:8892/health"
                        : c == 'h' || c == 'H' ? "http://GW_HOST_PLACEHOLDER:8891/"
                                               : "http://1.1.1.1/";
      HTTPClient probe;
      probe.begin(url);
      probe.setTimeout(10000);
      int pc = probe.GET();
      Serial.printf("[talk] probe %s -> HTTP %d\n", url, pc);
      probe.end();
    }
  }
  delay(5);
}
