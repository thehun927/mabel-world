# mabel-world

A sensor-rich physical foundation for Mabel — small nodes that let her perceive
and act in the house, starting with light.

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

Built by Attila Gyurovszky and Mabel.
