#!/usr/bin/env python3
"""mabel-voice-stream — LAB streaming front-end for the accelerated Qwen3-TTS engine.

NOT production. Production remains mabel-voice (/v1/say, whole-WAV) untouched.
Feature flag: generation is refused unless MABEL_STREAM_ENABLE=1.

Protocol: POST /v1/say-stream {text, language?, chunk_size?} -> NDJSON stream
  {"type":"start","format":{"codec":"pcm_s16le","sample_rate":24000,"channels":1},...}
  {"type":"frame","seq":n,"pcm_b64":"...","samples":k,"audio_seconds":f}
  {"type":"end","frames":k,"audio_seconds":f,"generation_seconds":f}
  {"type":"error","message":"..."}
Auth: Authorization: Bearer <MABEL_STREAM_API_TOKEN> (lab-local token file).
Safety (mirrors production mabel-voice):
  - Model is hard-pinned to Qwen/Qwen3-TTS-12Hz-1.7B-Base. No substitution, ever:
    a failed load is an error event, not a fallback.
  - Refuses to load unless >= MIN_FREE_VRAM_MIB is free (never fight prod for the card).
  - One generation at a time (async lock). Model unloads after IDLE_UNLOAD_SECONDS.
  - Generation params mirror production: temp 0.75, top_k 50, top_p 1.0,
    repetition_penalty 1.05, max_new_tokens 8192 (subtalker temp is wrapper-internal).
"""
import asyncio, base64, json, os, time
import numpy as np
import torch
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

os.environ.setdefault("HF_HUB_OFFLINE", "1")

MODEL_ID = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"   # hard pin — 1.7B only, no substitution
VOICE_DIR = os.path.expanduser("~/Projects/Mabel/mabel-voice/voices/mabel")
REF_AUDIO = os.path.join(VOICE_DIR, "reference.wav")
REF_TEXT_PATH = os.path.join(VOICE_DIR, "reference.txt")
ENABLED = os.environ.get("MABEL_STREAM_ENABLE", "0") == "1"
TOKEN = os.environ.get("MABEL_STREAM_API_TOKEN", "")
IDLE_UNLOAD_SECONDS = int(os.environ.get("MABEL_STREAM_IDLE_UNLOAD", "300"))
MIN_FREE_VRAM_MIB = int(os.environ.get("MABEL_STREAM_MIN_FREE_VRAM_MIB", "5200"))
GEN = dict(do_sample=True, temperature=0.75, top_k=50, top_p=1.0,
           repetition_penalty=1.05, max_new_tokens=8192)
SAMPLE_RATE = 24000

app = FastAPI(title="mabel-voice-stream (lab)")
_state = {"model": None, "last_used": 0.0, "load_seconds": None}
_lock = asyncio.Lock()

def _ref_text() -> str:
    with open(REF_TEXT_PATH) as f:
        return f.read().strip()

def _free_vram_mib() -> int:
    if not torch.cuda.is_available():
        return 0
    free, _total = torch.cuda.mem_get_info()
    return int(free // 2**20)

def _ensure_model():
    if _state["model"] is not None:
        return _state["model"]
    from faster_qwen3_tts import FasterQwen3TTS
    free = _free_vram_mib()
    if free < MIN_FREE_VRAM_MIB:
        raise RuntimeError(
            f"refusing to load {MODEL_ID}: only {free} MiB VRAM free "
            f"(need {MIN_FREE_VRAM_MIB}); another engine is resident")
    t0 = time.perf_counter()
    model = FasterQwen3TTS.from_pretrained(MODEL_ID)  # OOM here = error, never a smaller model
    try:
        model.warmup(prefill_len=100)
    except Exception:
        pass
    _state["model"] = model
    _state["load_seconds"] = round(time.perf_counter() - t0, 2)
    _state["last_used"] = time.monotonic()
    return model

def _unload():
    if _state["model"] is not None:
        _state["model"] = None
        torch.cuda.empty_cache()

async def _idle_loop():
    while True:
        await asyncio.sleep(15)
        if (_state["model"] is not None
                and time.monotonic() - _state["last_used"] > IDLE_UNLOAD_SECONDS):
            async with _lock:
                _unload()

@app.on_event("startup")
async def _startup():
    asyncio.create_task(_idle_loop())

def _auth(header: str | None):
    if not TOKEN or header != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")

@app.get("/health")
async def health():
    return {"status": "ok", "service": "mabel-voice-stream", "lab": True,
            "enabled": ENABLED, "model": MODEL_ID, "clone_mode": "icl",
            "model_loaded": _state["model"] is not None,
            "model_load_seconds": _state["load_seconds"],
            "free_vram_MiB": _free_vram_mib(),
            "wav_endpoint_untouched": "mabel-voice :8765 /v1/say"}

@app.post("/v1/say-stream")
async def say_stream(request: Request, authorization: str | None = Header(default=None)):
    _auth(authorization)
    if not ENABLED:
        raise HTTPException(status_code=403, detail="streaming disabled (MABEL_STREAM_ENABLE != 1)")
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    if len(text) > 2000:
        raise HTTPException(status_code=400, detail="text too long")
    language = body.get("language", "English")
    chunk_size = int(body.get("chunk_size", 8))
    chunk_size = max(2, min(chunk_size, 32))

    async def gen():
        def ev(obj):
            return (json.dumps(obj) + "\n").encode()
        if _lock.locked():
            yield ev({"type": "error", "message": "busy: a generation is already in progress"})
            return
        async with _lock:
            try:
                model = await asyncio.to_thread(_ensure_model)
            except Exception as e:
                yield ev({"type": "error", "message": f"model load failed: {e}"})
                return
            yield ev({"type": "start",
                      "format": {"codec": "pcm_s16le", "sample_rate": SAMPLE_RATE, "channels": 1},
                      "text_chars": len(text), "chunk_size": chunk_size, "model": MODEL_ID})
            seq = 0
            total_samples = 0
            t0 = time.perf_counter()
            try:
                stream = model.generate_voice_clone_streaming(
                    text=text, language=language, ref_audio=REF_AUDIO,
                    ref_text=_ref_text(), chunk_size=chunk_size, **GEN)
                for audio_chunk, sr, _timing in stream:
                    if await request.is_disconnected():
                        break  # client cancelled: stop generating, free the GPU
                    a = np.asarray(audio_chunk, dtype=np.float32)
                    pcm = (np.clip(a, -1.0, 1.0) * 32767).astype("<i2")
                    total_samples += len(a)
                    seq += 1
                    yield ev({"type": "frame", "seq": seq,
                              "pcm_b64": base64.b64encode(pcm.tobytes()).decode(),
                              "samples": int(len(a)),
                              "audio_seconds": round(len(a) / sr, 3)})
            except Exception as e:
                yield ev({"type": "error", "message": f"generation failed: {e}"})
                return
            finally:
                _state["last_used"] = time.monotonic()
            yield ev({"type": "end", "frames": seq,
                      "audio_seconds": round(total_samples / SAMPLE_RATE, 3),
                      "generation_seconds": round(time.perf_counter() - t0, 3)})

    return StreamingResponse(gen(), media_type="application/x-ndjson")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("MABEL_STREAM_PORT", "8767")))
