# Fény firmware v2 — office-01 camera node

Firmware for **Fény**, a DFRobot DFR1154 (ESP32-S3 + OV3660 camera) sensor
node. Fény is the first node of mabel-world: she watches one room, measures
its light, and reports both to the gateway in `../../gateway/`.

## What it does

- **Light observations** — reads the onboard LTR-308 ambient light sensor
  (I2C 0x53, SDA GPIO8 / SCL GPIO9) and POSTs a JSON observation to the
  gateway listener every 10 s:

  ```json
  {"node":"office-01","type":"observation","uptime_s":1234,"ip":"...","als_raw":567}
  ```

  `als_raw` is `-1` when no sensor answers (the firmware keeps running —
  a camera with no light reading is still a camera).
- **Stills** — every 60 s, captures a VGA JPEG and POSTs it as a
  base64-in-JSON `{"type":"still", ...}` message, streamed over a
  hand-rolled chunked HTTP request (the base64 payload is ~2× the frame
  buffer, so it is encoded and sent in pieces rather than held in RAM).
- **Camera web server** — the stock ESP32 CameraWebServer UI is kept for
  setup and debugging.

Working framesize is VGA: QVGA proved too coarse to describe a room from,
and UXGA stills stalled the frame loop for seconds at a time.

## Configuration

Three placeholders at the top of `FenyCamera_v2.ino` must be filled in
before building:

| Placeholder | Meaning |
|---|---|
| `__WIFI_SSID__` / `__WIFI_PASSWORD__` | The LAN the node lives on |
| `__GW_HOST__` | Host running `gateway/listener.py` (port 8891) |

Orientation: Fény's housing mounts her **flipped** (USB-C at the top).
Set `FENY_FLIPPED` to `1` in the sketch once the mount is confirmed and
the firmware applies `vflip`/`hmirror` to match.

## Build

arduino-cli, ESP32 core 3.3.12:

```
arduino-cli compile \
  --fqbn esp32:esp32:esp32s3:CDCOnBoot=cdc,PSRAM=opi,FlashSize=16M,PartitionScheme=app3M_fat9M_16MB \
  FenyCamera_v2
```

`camera_pins.h` carries the custom `CAMERA_MODEL_DFR1154_AICAM` pin block
for this board (XCLK 5, SIOD 8, SIOC 9, D0–D7 = 16/18/21/17/14/7/6/4,
VSYNC 1, HREF 2, PCLK 15).

## Lineage

v1 was the stock CameraWebServer sketch — it served frames on request and
reported nothing. v2 (2026-10-30, during the Meta Global AI Developer
Hackathon window) is the version that makes Fény a *node* instead of a
webcam: she pushes her own observations to the gateway, unsolicited,
whether or not anyone is watching.
