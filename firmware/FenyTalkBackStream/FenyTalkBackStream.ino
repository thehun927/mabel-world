// FenyTalkBackStream — v4.3 LAB build: the streaming round trip.
//
// !!! This is NOT the demo build. FenyTalkBack v3 (whole-WAV) remains the
// proven path for the hackathon. v4 is its streaming sibling, behind the
// lab flag: it talks to the feny-stream relay on the gateway (:8893),
// which relays PCM frames from KING's mabel-voice-stream endpoint. !!!
//
// Press BOOT (or send 'a' over serial) and Fény:
//   1. records REC_SECONDS from her PDM microphone (v3's proven recipe),
//   2. POSTs the WAV to the relay (STT -> compose -> streamed voice),
//   3. plays the reply INCREMENTALLY: NDJSON events arrive over a
//      close-delimited HTTP response; each frame's base64 PCM is decoded
//      into a PSRAM ring buffer. A dedicated player task feeds the I2S
//      speaker from the ring, so network parsing can never starve the
//      DAC (v4.1's in-loop player cut out whenever a burst of frames was
//      being drained — measured on the bench, Nov 3).
//   BOOT during playback = barge-in: cancel the stream, flush the ring,
//   and listen again IMMEDIATELY (the interrupted turn's telemetry is
//   deferred until after the new turn, so the mic opens at once —
//   v4.1 lost the head of the follow-up question to telemetry latency).
//   A stream that breaks mid-reply plays out whatever arrived and
//   returns to idle — no hang, no reboot.
//   After every turn Fény POSTs device-side telemetry to the relay:
//   press->first-sound and record-end->first-sound, measured HERE.
//
// Mic recipe follows v3 exactly: pinMode(41, OUTPUT) +
// setPinsPdmRx(38, 39) + recordWAV(), and NO i2s.end() after recordWAV
// (it asserts in heap_caps_free and reboots the board — Nov 1 lesson).
// Network reads are BLOCK reads (v3-style); byte-at-a-time read() on
// WiFiClient starved the first bench build (~8 s lost, Nov 3).
//
// Secrets live in the Mac-local feny_secrets.h beside this sketch
// (FENY_WIFI_SSID / FENY_WIFI_PASS / FENY_STREAM_TOKEN). Repo copies
// carry placeholders only — never commit the real values.

#include <ESP_I2S.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <WiFi.h>

#include "feny_secrets.h"

#define ASK_STREAM_URL "http://GW_HOST_PLACEHOLDER:8893/ask-stream"
#define TELEMETRY_URL "http://GW_HOST_PLACEHOLDER:8893/telemetry"
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
// Playback volume is a RUNTIME setting (v4.3): the relay can order a
// new one mid-stream via a {"type":"control","volume_percent":N} event
// (Fény's self-command channel), and it persists in NVS across reboots.
#define DEFAULT_VOLUME_PCT 10
static volatile int volumePct = DEFAULT_VOLUME_PCT;
static Preferences prefs;

static void saveVolumePct() {
  if (prefs.begin("feny", false)) {
    prefs.putInt("vol", (int)volumePct);
    prefs.end();
  }
}

// Streaming player tuning
#define RING_SAMPLES (PLAY_RATE * 6)     // 6 s of mono audio in PSRAM
#define START_SAMPLES (15360 * 2)        // begin playback with ~2 frames buffered (~1.28 s)
#define WRITE_FRAMES 512                 // I2S write granularity, as in v3
#define LINE_CAP 46080                   // one NDJSON frame line: 15360 samples -> ~41 KB base64
#define STREAM_IDLE_TIMEOUT_MS 8000      // no bytes for this long = broken stream
#define STREAM_TOTAL_TIMEOUT_MS 120000

I2SClass i2s;     // microphone (PDM RX)
I2SClass i2sSpk;  // speaker (STD TX) — separate instance, as in v3

static void led(bool on) { digitalWrite(LED_PIN, on ? HIGH : LOW); }

static void blink(int n, int ms) {
  for (int i = 0; i < n; i++) {
    led(true);
    delay(ms);
    led(false);
    delay(ms);
  }
}

static void wifiUp() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.print("[stream] WiFi connecting");
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
    Serial.printf("[stream] WiFi up: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("[stream] WiFi FAILED");
  }
}

// ---------------------------------------------------------------- record
// Identical to v3's recordAsk: PDM RX via the library helper, peak
// normalize in place (cap x12, target ~0.85 FS). Caller frees.
static uint8_t *recordAsk(size_t *outSize) {
  i2s.setPinsPdmRx(MIC_CLK, MIC_DATA);
  if (!i2s.begin(I2S_MODE_PDM_RX, REC_RATE, I2S_DATA_BIT_WIDTH_16BIT,
                 I2S_SLOT_MODE_MONO)) {
    Serial.println("[stream] PDM RX begin FAILED");
    return nullptr;
  }
  Serial.printf("[stream] listening for %d s...\n", REC_SECONDS);
  led(true);
  size_t wavSize = 0;
  uint8_t *wav = i2s.recordWAV(REC_SECONDS, &wavSize);
  led(false);
  // NO i2s.end() here — see the header note (v3, Nov 1).
  if (!wav || wavSize <= 44) {
    Serial.println("[stream] recordWAV FAILED");
    return nullptr;
  }
  int16_t *pcm = (int16_t *)(wav + 44);
  size_t n = (wavSize - 44) / 2;
  int32_t peak = 0;
  for (size_t i = 0; i < n; i++) {
    int32_t v = pcm[i] < 0 ? -(int32_t)pcm[i] : pcm[i];
    if (v > peak) peak = v;
  }
  Serial.printf("[stream] mic peak: %d (%.1f%% FS)\n", (int)peak,
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
    Serial.printf("[stream] normalized x%.1f\n", g);
  }
  *outSize = wavSize;
  return wav;
}

// ---------------------------------------------------------------- ring
// Written by the network loop (core 1), read by the player task (core 0).
// All shared state moves under a spinlock.
static int16_t *ring = nullptr;
static size_t ringHead = 0;
static size_t ringTail = 0;
static size_t ringCount = 0;
static portMUX_TYPE ringMux = portMUX_INITIALIZER_UNLOCKED;

static void ringReset() {
  portENTER_CRITICAL(&ringMux);
  ringHead = ringTail = ringCount = 0;
  portEXIT_CRITICAL(&ringMux);
}

static void ringPush(int16_t s) {
  portENTER_CRITICAL(&ringMux);
  if (ringCount < RING_SAMPLES) {
    ring[ringHead] = s;
    ringHead = (ringHead + 1) % RING_SAMPLES;
    ringCount++;
  }
  portEXIT_CRITICAL(&ringMux);
}

static size_t ringLevel() {
  portENTER_CRITICAL(&ringMux);
  size_t c = ringCount;
  portEXIT_CRITICAL(&ringMux);
  return c;
}

// Pop up to n samples; returns how many were actually popped.
static size_t ringPopN(int16_t *dst, size_t n) {
  portENTER_CRITICAL(&ringMux);
  size_t got = 0;
  while (got < n && ringCount > 0) {
    dst[got++] = ring[ringTail];
    ringTail = (ringTail + 1) % RING_SAMPLES;
    ringCount--;
  }
  portEXIT_CRITICAL(&ringMux);
  return got;
}

// ------------------------------------------------------- base64 -> ring
static int b64val(char c) {
  if (c >= 'A' && c <= 'Z') return c - 'A';
  if (c >= 'a' && c <= 'z') return c - 'a' + 26;
  if (c >= '0' && c <= '9') return c - '0' + 52;
  if (c == '+') return 62;
  if (c == '/') return 63;
  return -1;  // '=' padding or anything else
}

// Decode a base64 span (PCM s16le) straight into the ring.
static void decodeFrameToRing(const char *b64, size_t len) {
  uint32_t acc = 0;
  int nbits = 0;
  uint8_t byteBuf[2];
  int nb = 0;
  for (size_t i = 0; i < len; i++) {
    int v = b64val(b64[i]);
    if (v < 0) continue;
    acc = (acc << 6) | (uint32_t)v;
    nbits += 6;
    if (nbits >= 8) {
      nbits -= 8;
      byteBuf[nb++] = (uint8_t)((acc >> nbits) & 0xFF);
      if (nb == 2) {
        ringPush((int16_t)(byteBuf[0] | (byteBuf[1] << 8)));
        nb = 0;
      }
    }
  }
}

// ------------------------------------------------------------- player
// The player task owns the speaker while a turn is live. The network
// loop sets playerActive; the task starts writing once START_SAMPLES
// are buffered (or the stream ended with anything at all), feeds
// silence through mid-stream underruns, and drains the ring after the
// end event. It never touches the network or the ring's write side.
static volatile bool playerActive = false;    // a turn's stream is live
static volatile bool playerEnded = false;     // "end" event seen (or stream closed)
static volatile bool playerCancel = false;    // barge-in: stop NOW
static volatile bool spkBegun = false;
static volatile unsigned long firstSoundAt = 0;
static volatile uint32_t samplesPlayed = 0;
static volatile int underruns = 0;

static void playerTask(void *arg) {
  static int16_t mono[WRITE_FRAMES];
  static int16_t out[WRITE_FRAMES * 2];
  bool playing = false;
  bool inUnderrun = false;
  for (;;) {
    if (!playerActive) {
      playing = false;
      inUnderrun = false;
      vTaskDelay(pdMS_TO_TICKS(5));
      continue;
    }
    if (playerCancel) {
      vTaskDelay(pdMS_TO_TICKS(5));
      continue;  // the network loop owns teardown; just don't write
    }
    if (!playing) {
      size_t lvl = ringLevel();
      if (spkBegun && (lvl >= START_SAMPLES || (playerEnded && lvl > 0))) {
        playing = true;
        Serial.printf("[stream] playback begins, ring=%u samples\n",
                      (unsigned)lvl);
      } else {
        vTaskDelay(pdMS_TO_TICKS(5));
        continue;
      }
    }
    size_t got = ringPopN(mono, WRITE_FRAMES);
    if (got > 0) {
      // Partial final chunk: pad the tail with silence so writes stay
      // one fixed size and the DAC timing never stutters.
      for (size_t i = got; i < WRITE_FRAMES; i++) mono[i] = 0;
      for (size_t i = 0; i < WRITE_FRAMES; i++) {
        int32_t v = (int32_t)mono[i] * volumePct / 100;
        if (v > 32767) v = 32767;
        if (v < -32768) v = -32768;
        out[i * 2] = (int16_t)v;
        out[i * 2 + 1] = (int16_t)v;
      }
      if (firstSoundAt == 0) firstSoundAt = millis();
      i2sSpk.write((uint8_t *)out, WRITE_FRAMES * 4);
      samplesPlayed += WRITE_FRAMES;
      inUnderrun = false;
    } else if (!playerEnded) {
      // Mid-stream underrun: keep the DAC fed with silence and wait.
      if (!inUnderrun) {
        underruns++;
        inUnderrun = true;
        Serial.println("[stream] underrun — silence while waiting");
      }
      memset(out, 0, sizeof(out));
      i2sSpk.write((uint8_t *)out, WRITE_FRAMES * 4);
      samplesPlayed += WRITE_FRAMES;
    } else {
      vTaskDelay(pdMS_TO_TICKS(5));  // ended and drained; idle until teardown
    }
  }
}

static bool beginSpeaker() {
  i2sSpk.setPins(SPK_BCLK, SPK_LRC, SPK_DOUT, -1, -1);
  if (!i2sSpk.begin(I2S_MODE_STD, PLAY_RATE, I2S_DATA_BIT_WIDTH_16BIT,
                    I2S_SLOT_MODE_STEREO)) {
    Serial.println("[stream] STD TX begin FAILED");
    return false;
  }
  led(true);
  return true;
}

// ------------------------------------------------------------- telemetry
static char pendingTelemetry[256] = "";

static void postTelemetryBody(const char *body) {
  HTTPClient http;
  http.begin(TELEMETRY_URL);
  http.setTimeout(5000);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", "Bearer " FENY_STREAM_TOKEN);
  int code = http.POST((uint8_t *)body, strlen(body));
  Serial.printf("[stream] telemetry -> HTTP %d\n", code);
  http.end();
}

static void sendTelemetry(unsigned long pressMs, unsigned long replyWaitMs,
                          int frames, const char *outcome, int underruns_,
                          uint32_t played) {
  char body[256];
  snprintf(body, sizeof(body),
           "{\"press_to_first_sound_ms\":%lu,\"reply_wait_ms\":%lu,"
           "\"frames\":%d,\"outcome\":\"%s\",\"underruns\":%d,"
           "\"audio_samples_played\":%u,\"volume_percent\":%d}",
           pressMs, replyWaitMs, frames, outcome, underruns_,
           (unsigned)played, (int)volumePct);
  if (pendingTelemetry[0]) {
    // A deferred report from an interrupted turn goes first.
    postTelemetryBody(pendingTelemetry);
    pendingTelemetry[0] = '\0';
  }
  postTelemetryBody(body);
}

// ------------------------------------------------------------- one turn
static bool pendingReask = false;  // set by barge-in; loop() starts a new turn

static void askStreamOnce(unsigned long pressMs) {
  wifiUp();
  if (WiFi.status() != WL_CONNECTED) return;
  size_t wavLen = 0;
  uint8_t *wav = recordAsk(&wavLen);
  if (!wav) return;
  unsigned long recordEndMs = millis();

  Serial.printf("[stream] asking relay (%u bytes)... rssi=%d\n",
                (unsigned)wavLen, (int)WiFi.RSSI());
  HTTPClient http;
  http.begin(ASK_STREAM_URL);
  http.setTimeout(STREAM_TOTAL_TIMEOUT_MS);
  http.addHeader("Content-Type", "audio/wav");
  http.addHeader("Authorization", "Bearer " FENY_STREAM_TOKEN);
  int code = http.POST(wav, wavLen);
  free(wav);
  if (code != 200) {
    Serial.printf("[stream] relay POST -> HTTP %d\n", code);
    http.end();
    blink(2, 120);
    sendTelemetry(0, 0, 0, "http_error", 0, 0);
    return;
  }

  WiFiClient *stream = http.getStreamPtr();
  ringReset();
  char *line = (char *)ps_malloc(LINE_CAP);
  if (!line) {
    Serial.println("[stream] no PSRAM for line buffer");
    http.end();
    return;
  }

  playerActive = false;
  playerEnded = false;
  playerCancel = false;
  spkBegun = false;
  firstSoundAt = 0;
  samplesPlayed = 0;
  underruns = 0;

  bool streamEnded = false;
  bool sawError = false;
  bool cancelled = false;
  int frames = 0;
  unsigned long lastDataMs = millis();
  unsigned long t0 = millis();
  size_t lineLen = 0;
  int lastBoot = digitalRead(BOOT_PIN);

  auto handleLine = [&](char *ln, size_t len) {
    ln[len] = '\0';
    if (strstr(ln, "\"type\":\"control\"") || strstr(ln, "\"type\": \"control\"")) {
      // Self-command channel: orders about Fény herself, applied the
      // moment they arrive — before this turn's speech, when the relay
      // sends them first.
      const char *key = strstr(ln, "\"volume_percent\":");
      if (key) {
        int pct = atoi(key + 17);
        if (pct < 0) pct = 0;
        if (pct > 100) pct = 100;
        volumePct = pct;
        saveVolumePct();
        Serial.printf("[stream] control: my volume is now %d%%\n", pct);
      }
    } else if (strstr(ln, "\"type\":\"start\"") || strstr(ln, "\"type\": \"start\"")) {
      Serial.println("[stream] start event");
      if (!spkBegun) {
        spkBegun = beginSpeaker();
        playerActive = true;
      }
    } else if (strstr(ln, "\"type\":\"frame\"") || strstr(ln, "\"type\": \"frame\"")) {
      const char *key = strstr(ln, "\"pcm_b64\":\"");
      if (!key) key = strstr(ln, "\"pcm_b64\": \"");
      if (key) {
        const char *start = strchr(key + 10, '"') + 1;
        const char *end = strchr(start, '"');
        if (end > start) {
          decodeFrameToRing(start, (size_t)(end - start));
          frames++;
          if (frames == 1)
            Serial.printf("[stream] first frame in ring at +%lums\n",
                          (unsigned long)(millis() - recordEndMs));
        }
      }
    } else if (strstr(ln, "\"type\":\"heard\"") || strstr(ln, "\"type\": \"heard\"")) {
      Serial.printf("[stream] heard event: %.120s\n", ln);
    } else if (strstr(ln, "\"type\":\"end\"") || strstr(ln, "\"type\": \"end\"")) {
      streamEnded = true;
      playerEnded = true;
      Serial.printf("[stream] end event after %d frames\n", frames);
    } else if (strstr(ln, "\"type\":\"error\"") || strstr(ln, "\"type\": \"error\"")) {
      sawError = true;
      playerEnded = true;
      Serial.printf("[stream] ERROR event: %.160s\n", ln);
    }
  };

  // Network pump: bytes -> lines -> ring. Playback is the task's job.
  while (true) {
    int b = digitalRead(BOOT_PIN);
    if (lastBoot == HIGH && b == LOW) {
      delay(30);
      if (digitalRead(BOOT_PIN) == LOW) {
        cancelled = true;
        pendingReask = true;
        playerCancel = true;
        Serial.println("[stream] BOOT during playback — cancelling");
        break;
      }
    }
    lastBoot = b;
    if (Serial.available()) {
      char c = Serial.read();
      if (c == 'x' || c == 'X') {
        cancelled = true;
        playerCancel = true;
        Serial.println("[stream] serial cancel");
        break;
      }
    }

    size_t avail = stream->available();
    if (avail) {
      lastDataMs = millis();
      static uint8_t netBuf[2048];
      while (avail > 0) {
        size_t want = avail > sizeof(netBuf) ? sizeof(netBuf) : avail;
        int n = stream->readBytes(netBuf, want);
        if (n <= 0) break;
        for (int i = 0; i < n; i++) {
          char ch = (char)netBuf[i];
          if (ch == '\n') {
            if (lineLen > 0) handleLine(line, lineLen);
            lineLen = 0;
          } else if (lineLen < LINE_CAP - 1) {
            line[lineLen++] = ch;
          }
        }
        avail = stream->available();
      }
    }

    if (cancelled) break;
    if (streamEnded && ringLevel() == 0 && frames > 0) {
      // Give the player a breath to notice the drain before teardown.
      delay(60);
      if (ringLevel() == 0) break;
    }
    bool connDone = !http.connected() && stream->available() == 0;
    if (connDone && !streamEnded) break;                      // broken stream
    if (millis() - lastDataMs > STREAM_IDLE_TIMEOUT_MS && !streamEnded) break;
    if (millis() - t0 > STREAM_TOTAL_TIMEOUT_MS) break;
    if (!avail) delay(2);
  }

  const char *outcome;
  if (cancelled) outcome = "interrupted";
  else if (sawError) outcome = "error";
  else if (streamEnded) outcome = "complete";
  else outcome = "broken";  // connection died / timed out before "end"

  playerActive = false;
  playerEnded = true;
  if (spkBegun) {
    delay(80);  // let the task push its final chunk into DMA
    i2sSpk.end();
    led(false);
  }
  http.end();
  free(line);
  Serial.printf("[stream] turn done: outcome=%s frames=%d underruns=%d\n",
                outcome, frames, (int)underruns);
  if (strcmp(outcome, "broken") == 0) blink(3, 120);
  if (frames == 0 && !cancelled) blink(2, 120);

  unsigned long replyWait = firstSoundAt ? (firstSoundAt - recordEndMs) : 0;
  unsigned long pressToSound = firstSoundAt ? (firstSoundAt - pressMs) : 0;
  if (pendingReask) {
    // Defer this turn's telemetry: the follow-up question matters more
    // than the report, and the mic must open NOW (v4.1 lesson).
    snprintf(pendingTelemetry, sizeof(pendingTelemetry),
             "{\"press_to_first_sound_ms\":%lu,\"reply_wait_ms\":%lu,"
             "\"frames\":%d,\"outcome\":\"%s\",\"underruns\":%d,"
             "\"audio_samples_played\":%u,\"volume_percent\":%d}",
             pressToSound, replyWait, frames, outcome, (int)underruns,
             (unsigned)samplesPlayed, (int)volumePct);
  } else {
    sendTelemetry(pressToSound, replyWait, frames, outcome, (int)underruns,
                  samplesPlayed);
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  pinMode(BOOT_PIN, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  pinMode(41, OUTPUT);  // per the reference recipe for this board's audio
  led(false);
  if (prefs.begin("feny", true)) {
    volumePct = prefs.getInt("vol", DEFAULT_VOLUME_PCT);
    prefs.end();
  }
  if (volumePct < 0) volumePct = 0;
  if (volumePct > 100) volumePct = 100;
  ring = (int16_t *)ps_malloc(RING_SAMPLES * sizeof(int16_t));
  if (!ring) Serial.println("[stream] ring alloc FAILED");
  xTaskCreatePinnedToCore(playerTask, "feny-player", 4096, nullptr, 2, nullptr, 0);
  Serial.println();
  Serial.printf("[stream] FenyTalkBackStream v4.3 (LAB streaming, self-command) — volume %d%% — BOOT or 'a' to ask, BOOT/'x' to interrupt\n", (int)volumePct);
  wifiUp();
}

void loop() {
  wifiUp();
  if (pendingReask) {
    pendingReask = false;
    askStreamOnce(millis());
    return;
  }
  static int lastBoot = HIGH;
  int b = digitalRead(BOOT_PIN);
  if (lastBoot == HIGH && b == LOW) {
    delay(30);
    if (digitalRead(BOOT_PIN) == LOW) askStreamOnce(millis());
  }
  lastBoot = b;
  if (Serial.available()) {
    char c = Serial.read();
    if (c == 'a' || c == 'A') askStreamOnce(millis());
  }
  delay(5);
}
