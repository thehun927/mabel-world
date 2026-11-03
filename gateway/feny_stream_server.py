#!/usr/bin/env python3
"""feny-stream — LAB streaming relay on mabel-gw (NOT the production feny-loop).

POST /ask-stream   body: WAV bytes (16 kHz mono PCM16 recorded by Feny's mic)
  -> NDJSON stream, relayed frame-by-frame from KING's mabel-voice-stream:
     {"type":"heard","transcript":...,"reply_text":...,"stt_seconds":f}
     {"type":"start", format metadata ...}          (from KING, untouched)
     {"type":"frame","seq":n,"pcm_b64":...}          (from KING, untouched)
     {"type":"end", ...}                             (from KING, untouched)
     {"type":"error","message":...}                  (either hop)
GET /health -> {"ok": true, ...}

No complete-WAV buffering anywhere: STT runs on the uploaded question, then
reply frames are relayed as they arrive. No re-encoding: frame events pass
through byte-identical. Client disconnect closes the KING request, which
cancels generation on KING (the interruption path).
Brain is compose() — the same rule-based composer as feny-loop v1.
Feature flag: this service simply is not running unless started; feny-loop
(:8892) remains the production path and is untouched.
"""
import http.client
import io
import json
import os
import threading
import time
import wave
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import numpy as np

HOME = os.path.expanduser("~/feny-stream")
TOKEN = open(os.path.join(HOME, "token.txt")).read().strip()
STREAM_TOKEN = open(os.path.join(HOME, "king_stream_token.txt")).read().strip()
KING_STREAM_HOST = "KING_STREAM_HOST_PLACEHOLDER"
KING_STREAM_PORT = 8768
MODEL_NAME = os.environ.get("FENY_STT_MODEL", "small")
LOG = open(os.path.join(HOME, "stream.jsonl"), "a", buffering=1)

from faster_whisper import WhisperModel

import feny_brain

_t0 = time.time()
model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")

# --- conversation memory (lab): recent exchanges, idle-expired.
# What she actually said is what gets remembered, rules and brain alike.
_mem_lock = threading.Lock()
_mem = {"turns": [], "last": 0.0}


def mem_history():
    idle_s, max_turns = feny_brain.session_config()
    now = time.time()
    with _mem_lock:
        if _mem["turns"] and now - _mem["last"] > idle_s:
            _mem["turns"] = []
        turns = _mem["turns"][-max_turns:]
    msgs = []
    for u, a in turns:
        msgs.append({"role": "user", "content": u})
        msgs.append({"role": "assistant", "content": a})
    return msgs


def mem_record(user, assistant):
    if not user or not assistant:
        return
    _idle, max_turns = feny_brain.session_config()
    with _mem_lock:
        _mem["turns"].append((user, assistant))
        _mem["turns"] = _mem["turns"][-max_turns:]
        _mem["last"] = time.time()


def mem_clear():
    with _mem_lock:
        _mem["turns"] = []
        _mem["last"] = 0.0


def mem_count():
    idle_s, _mt = feny_brain.session_config()
    with _mem_lock:
        if _mem["turns"] and time.time() - _mem["last"] > idle_s:
            return 0
        return len(_mem["turns"])


# Last known device state, synced from Feny's own telemetry. The brain
# is told the current volume so "a little bit" has a starting point.
DEV = {"volume_percent": 10}

print(f"[feny-stream] STT model '{MODEL_NAME}' loaded in {time.time() - _t0:.1f}s", flush=True)
_stt_lock = threading.Lock()


def decode_wav(wav_bytes):
    """PCM16 WAV -> float32 mono 16 kHz numpy array (same bypass as feny-loop)."""
    with wave.open(io.BytesIO(wav_bytes)) as w:
        n, ch, sw, rate = (w.getnframes(), w.getnchannels(),
                           w.getsampwidth(), w.getframerate())
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError(f"expected PCM16 WAV, got sampwidth {sw}")
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if rate != 16000 and len(a):
        n_out = int(len(a) * 16000 / rate)
        a = np.interp(np.linspace(0, 1, n_out, endpoint=False),
                      np.linspace(0, 1, len(a), endpoint=False), a).astype(np.float32)
    return a


def transcribe(wav_bytes):
    audio = decode_wav(wav_bytes)
    with _stt_lock:
        segs, _info = model.transcribe(audio, beam_size=1, language="en", vad_filter=False)
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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def _send_chunk(self, data: bytes):
        # Close-delimited body (HTTP/1.0): Feny reads lines off the raw
        # stream and the end of the body is the connection closing.
        self.wfile.write(data)
        self.wfile.flush()

    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({"ok": True, "service": "feny-stream", "lab": True,
                               "stt_model": MODEL_NAME,
                               "king_stream": f"http://{KING_STREAM_HOST}:{KING_STREAM_PORT}/v1/say-stream", "brain": feny_brain.available(), "session_turns": mem_count(), "volume_percent": DEV["volume_percent"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)


    def _pump_brain(self, text, t_start, stt_s, upload_s, history=None):
        """Brain route: stream_turn yields ("phrase", text) and
        ("action", {...}) items in true order. Phrases are synthesized
        on KING and forwarded as ONE continuous device stream (first
        start kept, per-phrase ends swallowed, one end at the end).
        Actions are executed inline: set_volume updates DEV and sends
        a control event down the same stream, ahead of any speech
        that follows it. Commits only once the first item exists;
        returns False with nothing sent if the brain failed before
        that, so the caller falls back to the rule path untouched."""
        gen = feny_brain.stream_turn(text, history, dict(DEV))
        try:
            item = next(gen)
        except StopIteration:
            return False
        except Exception:
            return False
        frames = 0
        ttfa = None
        phrases = 0
        actions = 0
        started = False
        assembled = []
        try:
            self._send_chunk((json.dumps(
                {"type": "heard", "transcript": text, "reply_text": "",
                 "brain": feny_brain.model_name(),
                 "stt_seconds": round(stt_s, 3)}) + "\n").encode())
            while item is not None:
                kind, val = item
                if kind == "action":
                    actions += 1
                    if val.get("name") == "set_volume":
                        try:
                            pct = int(val.get("args", {}).get(
                                "percent", DEV["volume_percent"]))
                        except Exception:
                            pct = DEV["volume_percent"]
                        pct = max(0, min(100, pct))
                        DEV["volume_percent"] = pct
                        self._send_chunk((json.dumps(
                            {"type": "control",
                             "volume_percent": pct}) + "\n").encode())
                        print(f"[feny-stream] control -> device volume {pct}%", flush=True)
                    else:
                        print(f"[feny-stream] unknown action ignored: {val}", flush=True)
                else:
                    phrase = val
                    phrases += 1
                    assembled.append(phrase)
                    king = None
                    try:
                        king = http.client.HTTPConnection(KING_STREAM_HOST, KING_STREAM_PORT, timeout=120)
                        king.request("POST", "/v1/say-stream",
                                     body=json.dumps({"text": phrase, "language": "English", "chunk_size": 8}),
                                     headers={"Authorization": f"Bearer {STREAM_TOKEN}",
                                              "Content-Type": "application/json"})
                        resp = king.getresponse()
                        if resp.status != 200:
                            print(f"[feny-stream] brain phrase KING HTTP {resp.status}", flush=True)
                        else:
                            while True:
                                line = resp.readline()
                                if not line:
                                    break
                                ev = None
                                if line.strip():
                                    try:
                                        ev = json.loads(line)
                                    except Exception:
                                        pass
                                if ev is not None:
                                    etype = ev.get("type")
                                    if etype == "frame":
                                        frames += 1
                                        if ttfa is None:
                                            ttfa = time.time() - t_start
                                    elif etype == "start":
                                        if started:
                                            continue
                                        started = True
                                    elif etype == "end":
                                        continue
                                    elif etype == "error":
                                        print(f"[feny-stream] brain phrase KING error: {str(ev)[:160]}", flush=True)
                                        continue
                                self._send_chunk(line if line.endswith(b"\n") else line + b"\n")
                    finally:
                        if king is not None:
                            try:
                                king.close()
                            except Exception:
                                pass
                if getattr(self, "_client_gone", False):
                    break
                try:
                    item = next(gen)
                except StopIteration:
                    item = None
            self._send_chunk((json.dumps(
                {"type": "end", "frames": frames,
                 "brain": feny_brain.model_name(), "phrases": phrases,
                 "actions": actions}) + "\n").encode())
        except (BrokenPipeError, ConnectionResetError):
            pass  # Feny hung up mid-brain-turn; KING closed above
        except Exception as e:
            print(f"[feny-stream] brain pump failed: {e!r}", flush=True)
            try:
                self._send_chunk((json.dumps(
                    {"type": "end", "frames": frames, "brain_error": True}) + "\n").encode())
            except Exception:
                pass
        finally:
            try:
                gen.close()
            except Exception:
                pass
            try:
                self.wfile.flush()
            except Exception:
                pass
            LOG.write(json.dumps(
                {"ts": datetime.now(ZoneInfo("America/New_York")).isoformat(),
                 "transcript": text, "reply_text": " ".join(assembled),
                 "reply_chars": len(" ".join(assembled)), "frames": frames,
                 "brain": feny_brain.model_name(), "phrases": phrases,
                 "actions": actions,
                 "volume_percent": DEV["volume_percent"],
                 "upload_seconds": round(upload_s, 3),
                 "stt_seconds": round(stt_s, 3),
                 "ttfa_from_request": round(ttfa, 3) if ttfa else None,
                 "total_seconds": round(time.time() - t_start, 3)}) + "\n")
        _assembled = " ".join(assembled)
        if _assembled:
            mem_record(text, _assembled)
        return True

    def do_POST(self):
        if self.path == "/reset-session":
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self.send_error(401)
                return
            mem_clear()
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/telemetry":
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self.send_error(401)
                return
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n) if n else b"{}"
            try:
                entry = json.loads(raw)
            except Exception:
                entry = {"unparsed": True}
            if isinstance(entry.get("volume_percent"), int):
                DEV["volume_percent"] = entry["volume_percent"]
            entry["kind"] = "telemetry"
            entry["ts"] = datetime.now(ZoneInfo("America/New_York")).isoformat()
            LOG.write(json.dumps(entry) + "\n")
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != "/ask-stream":
            self.send_error(404)
            return
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.send_error(401)
            return
        t_start = time.time()
        n = int(self.headers.get("Content-Length", 0))
        wav_bytes = self.rfile.read(n) if n else b""
        upload_s = time.time() - t_start
        try:
            text = transcribe(wav_bytes)
        except Exception as e:
            self.send_error(400, f"stt failed: {e}")
            return
        stt_s = time.time() - t_start
        reply = compose(text)
        record_turn = bool(text)
        _low = text.lower()
        if any(p in _low for p in ("forget everything", "forget all that", "start over")):
            mem_clear()
            reply = "Done. Fresh slate. Ask me anything."
            record_turn = False

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        _force_brain = any(w in _low for w in
                           ("volume", "louder", "quieter", "mute", "unmute",
                            "turn up", "turn down"))
        if feny_brain.available() and (reply.startswith("You said:") or _force_brain):
            if self._pump_brain(text, t_start, stt_s, upload_s, mem_history()):
                return

        frames = 0
        ttfa = None
        king = None
        try:
            self._send_chunk((json.dumps(
                {"type": "heard", "transcript": text, "reply_text": reply,
                 "stt_seconds": round(stt_s, 3)}) + "\n").encode())
            king = http.client.HTTPConnection(KING_STREAM_HOST, KING_STREAM_PORT, timeout=120)
            king.request("POST", "/v1/say-stream",
                         body=json.dumps({"text": reply, "language": "English", "chunk_size": 8}),
                         headers={"Authorization": f"Bearer {STREAM_TOKEN}",
                                  "Content-Type": "application/json"})
            resp = king.getresponse()
            if resp.status != 200:
                self._send_chunk((json.dumps(
                    {"type": "error", "message": f"KING stream HTTP {resp.status}"}) + "\n").encode())
            else:
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    if line.strip():
                        try:
                            ev = json.loads(line)
                            if ev.get("type") == "frame":
                                frames += 1
                                if ttfa is None:
                                    ttfa = time.time() - t_start
                        except Exception:
                            pass
                    self._send_chunk(line if line.endswith(b"\n") else line + b"\n")
        except (BrokenPipeError, ConnectionResetError):
            pass  # Feny hung up: closing KING below cancels generation there
        except Exception as e:
            try:
                self._send_chunk((json.dumps(
                    {"type": "error", "message": f"relay failed: {e}"}) + "\n").encode())
            except Exception:
                pass
        finally:
            if king is not None:
                try:
                    king.close()
                except Exception:
                    pass
            try:
                self.wfile.flush()
            except Exception:
                pass
            if record_turn:
                mem_record(text, reply)
            LOG.write(json.dumps(
                {"ts": datetime.now(ZoneInfo("America/New_York")).isoformat(),
                 "transcript": text, "reply_chars": len(reply), "frames": frames,
                 "upload_seconds": round(upload_s, 3),
                 "stt_seconds": round(stt_s, 3),
                 "ttfa_from_request": round(ttfa, 3) if ttfa else None,
                 "total_seconds": round(time.time() - t_start, 3)}) + "\n")


if __name__ == "__main__":
    port = int(os.environ.get("FENY_STREAM_PORT", "8893"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
