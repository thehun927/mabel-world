import json, os, sys, time, urllib.request

HOME = os.path.expanduser("~/feny-stream")
TOKEN = open(os.path.join(HOME, "token.txt")).read().strip()
WAV = sys.argv[1]
ABORT_AFTER = int(sys.argv[2]) if len(sys.argv) > 2 else 0
data = open(WAV, "rb").read()
req = urllib.request.Request(
    "http://127.0.0.1:8893/ask-stream", data=data,
    headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "audio/wav"})
t0 = time.perf_counter()
frames = 0
try:
    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw in resp:
            line = raw.strip()
            if not line:
                continue
            ev = json.loads(line)
            now = time.perf_counter() - t0
            ty = ev.get("type")
            if ty == "heard":
                print(f"t={now:.3f}s HEARD stt={ev['stt_seconds']}s transcript={ev['transcript']!r} reply={ev['reply_text']!r}")
            elif ty == "start":
                print(f"t={now:.3f}s START {ev['format']}")
            elif ty == "frame":
                frames += 1
                if frames == 1:
                    print(f"t={now:.3f}s FIRST FRAME seq={ev['seq']} samples={ev['samples']}  <-- first audible sound (if buffered at 1 frame)")
                if ABORT_AFTER and frames >= ABORT_AFTER:
                    print(f"t={now:.3f}s ABORT after {frames} frames (simulated button-press interruption)")
                    break
            elif ty == "end":
                print(f"t={now:.3f}s END frames={ev['frames']} audio={ev['audio_seconds']}s gen={ev['generation_seconds']}s")
            elif ty == "error":
                print(f"t={now:.3f}s ERROR {ev['message']}")
except Exception as e:
    print(f"client exception: {e}")
print(f"client done in {time.perf_counter()-t0:.3f}s, frames={frames}")
