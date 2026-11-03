# mabel-world

A sensor-rich physical foundation for Mabel — small nodes that let her perceive
and act in the house, starting with light.

## Prior work

This repo contains one foundation commit from before the hackathon window
(Oct 24, 2026): the mabel-world gateway listener, its systemd unit, and this
README. The board firmware from that phase was never committed and lives
only on the device. Everything else in this repository was built during
the Meta AI Developer Hackathon window, Oct 26 - Nov 4, 2026.

## Node: office-01

DFRobot DFR1154 (ESP32-S3, 8 MB PSRAM) running MicroPython. Reads its LTR-308
ambient light sensor and POSTs an observation every 10 seconds:

```json
{"node": "office-01", "ip": "192.168.254.112", "uptime_s": 16005, "als_raw": 87}
```

It also serves a small status page and `/state` on the LAN. The board firmware
currently lives on the device (flashed 2026-10-19); pulling it into this repo
is the next step. "office-01" is an address, not a name — her real name comes
later, deliberately.

## Gateway (mabel-gw)

`gateway/listener.py` — a tiny stdlib HTTP listener on port 8891. It stamps
each observation with `gw_received_at` and appends it to a daily
`observations-YYYY-MM-DD.jsonl`. Runs supervised as the systemd service in
`gateway/mabel-world-listener.service` (service-ized 2026-10-24; before that
it was a `nohup` process).

## Node: Fény — voice (hackathon build)

The same DFR1154 board, grown from a light sensor into a voice: press BOOT,
ask a question out loud, and Mabel answers aloud in her own cloned voice.
Camera firmware for the same board lives in `firmware/FenyCamera_v2/`.

### The streaming loop — `firmware/FenyTalkBackStream/` (v4.3, the film build)

1. BOOT press records 5 s from the onboard PDM microphone, peak-normalized
   on-device.
2. The WAV goes to the gateway relay (`gateway/feny_stream_server.py`,
   port 8893), which transcribes it locally with faster-whisper.
3. A small rule layer answers instant cases (time, name, greetings).
   Everything else goes to the brain (`gateway/feny_brain.py`): Muse Spark
   on the Meta Model API, run at minimal reasoning effort for voice latency,
   with conversation memory — the last 6 exchanges, a 10-minute idle expiry,
   and a spoken reset ("forget everything").
4. The reply streams phrase by phrase to a TTS service
   (`voice/stream_server.py`: Qwen3-TTS 1.7B, a voice clone, on a GPU host);
   PCM frames are relayed to the device as they are produced.
5. The firmware plays from a PSRAM ring buffer in a dedicated FreeRTOS task,
   starting after ~2 frames. BOOT during playback is a barge-in: playback
   cancels and the microphone reopens immediately. Each turn ends with
   telemetry (first-sound latency, buffer underruns) posted back to the relay.

### Self-command

The brain has one tool, `set_volume(percent)`. When it fires, a control event
rides the same response stream and the firmware applies it *mid-answer* —
"Mabel, turn up your volume to 25%" — then she confirms at the new volume.
The setting persists in NVS across reboots. Relative requests work too:
"a little" is ±5 points, "a lot" is ±25.

### The classic loop (fallback)

`firmware/FenyTalkBack/` (v3) + `gateway/feny_loop_server.py` (port 8892) is
the whole-file predecessor: record → transcribe → compose → render → play,
about 9–10 s end to end. Kept as the known-good fallback the streaming build
was developed against.

### Honest limits

- Home LAN only: one hardcoded Wi-Fi SSID, no cloud relay.
- USB-powered; a battery deck is future work.
- The voice identity is a local clone reference that is **not** in this repo.
- Secrets (Wi-Fi credentials, bearer tokens, API key) live only in local
  files on the machines that run each piece. The repo carries placeholders —
  see `feny_secrets.example.h` and `.gitignore`.

## House rules

- Voice, not eyes, where little eyes sleep.
- Short rolling buffers; no archives.
- Nose, not eyes.

## Status

- First node live 2026-10-19: light verified (245 -> 7 under a hand -> 30,013
  under a flashlight), speaker chime E5-A5-E6 heard.
- 2026-10-24: listener service-ized under systemd; hand-cup and desk-lamp
  tests re-verified live through the new service (17 -> 12 cupped in a dark
  office; 87 under the full lamp).
- Build window: Meta Global AI Developer Hackathon, Oct 26 - Nov 4, 2026
  (Indie Dev track). Camera joins the MVP next.
- 2026-11-03: streaming voice loop live end to end — transcription, Spark
  brain with conversation memory, streamed cloned-voice replies, barge-in,
  and the volume self-command all verified at the desk. Filming build:
  FenyTalkBackStream v4.3.

Built by Attila Gyurovszky and Mabel.
