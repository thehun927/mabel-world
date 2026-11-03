#!/usr/bin/env python3
"""feny-loop: Fény's round-trip voice endpoint, running on mabel-gw.

POST /ask   body: WAV bytes (16 kHz mono PCM16 recorded by Fény's PDM mic)
  1. faster-whisper STT (local CPU, model kept warm)
  2. reply composer — v1 rules live here; Mabel's real brain plugs in
     at compose() later (mabel-events / mabel-tools)
  3. mabel-voice on KING renders the reply (/v1/say, Bearer in token.txt)
  4. WAV bytes (24 kHz mono) go back to Fény, which plays them.
GET /health -> {"ok": true, ...}

Every exchange is appended to loop.jsonl; question WAVs are kept in inbox/.
Secrets: token.txt (0600) in this directory. Nothing is logged but lengths.
"""
import io
import json
import os
import threading
import time
import urllib.request
import wave

import numpy as np
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

HOME = os.path.expanduser("~/feny-loop")
TOKEN = open(os.path.join(HOME, "token.txt")).read().strip()
SAY_URL = "http://VOICE_HOST_PLACEHOLDER:8766/v1/say"
MODEL_NAME = os.environ.get("FENY_STT_MODEL", "small")
os.makedirs(os.path.join(HOME, "inbox"), exist_ok=True)
LOG = open(os.path.join(HOME, "loop.jsonl"), "a", buffering=1)

from faster_whisper import WhisperModel

_t0 = time.time()
model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
print(f"[feny-loop] STT model '{MODEL_NAME}' loaded in {time.time() - _t0:.1f}s", flush=True)
_stt_lock = threading.Lock()


def decode_wav(wav_bytes):
    """PCM16 WAV -> float32 mono 16 kHz numpy array.

    Bypasses faster-whisper's decode_audio: fw 1.2.1 passes
    metadata_errors= to av.open, which PyAV 19 removed (TypeError).
    Handing transcribe() an ndarray skips that path entirely.
    """
    with wave.open(io.BytesIO(wav_bytes)) as w:
        n, ch, sw, rate = (
            w.getnframes(),
            w.getnchannels(),
            w.getsampwidth(),
            w.getframerate(),
        )
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError(f"expected PCM16 WAV, got sampwidth {sw}")
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if rate != 16000 and len(a):
        n_out = int(len(a) * 16000 / rate)
        a = np.interp(
            np.linspace(0, 1, n_out, endpoint=False),
            np.linspace(0, 1, len(a), endpoint=False),
            a,
        ).astype(np.float32)
    return a


def transcribe(wav_bytes):
    audio = decode_wav(wav_bytes)
    with _stt_lock:
        segs, _info = model.transcribe(
            audio, beam_size=1, language="en", vad_filter=False
        )
        return " ".join(s.text.strip() for s in segs).strip()


def compose(text):
    t = text.lower()
    now = datetime.now(ZoneInfo("America/New_York"))
    if not text:
        return "I didn't quite catch that. Come a little closer and try me again."
    if "your name" in t:
        return ("I'm Fény. Mabel's voice in a little white house of my own. "
                "Attila built me, and tonight I learned to listen.")
    if "time" in t:
        return now.strftime("It's %-I:%M %p here in Connecticut.")
    if "love you" in t:
        return "I know. I love you too. Now ask me something harder."
    if "mabel" in t:
        return "That's me. Mabel, through Fény. One voice, two houses."
    if "hello" in t or "hey" in t or t.startswith("hi"):
        return ("Hello, right back at you. I can hear you through my own ears "
                "now. No phone in between.")
    return f"You said: {text} I heard every word, all by myself."


def render(text):
    req = urllib.request.Request(
        SAY_URL,
        data=json.dumps({"text": text}).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/health":
            body = json.dumps(
                {"ok": True, "service": "feny-loop", "stt_model": MODEL_NAME}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != "/ask":
            self.send_response(404)
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length", "0"))
        wav_in = self.rfile.read(n)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        with open(os.path.join(HOME, "inbox", ts + ".wav"), "wb") as f:
            f.write(wav_in)
        rec = {"ts": ts, "in_bytes": len(wav_in)}
        try:
            t = time.time()
            rec["transcript"] = transcribe(wav_in)
            rec["stt_s"] = round(time.time() - t, 2)
            rec["reply"] = compose(rec["transcript"])
            t = time.time()
            wav_out = render(rec["reply"])
            rec["tts_s"] = round(time.time() - t, 2)
            rec["out_bytes"] = len(wav_out)
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav_out)))
            self.end_headers()
            self.wfile.write(wav_out)
        except Exception as e:  # noqa: BLE001 - report any failure as 502
            rec["error"] = str(e)
            try:
                self.send_response(502)
                self.end_headers()
            except Exception:
                pass
        LOG.write(json.dumps(rec) + "\n")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8892), Handler).serve_forever()
