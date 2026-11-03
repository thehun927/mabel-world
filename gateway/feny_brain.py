"""Fény's brain (LAB): streamed answers + self-commands from the
Meta Model API (Muse Spark).

Reads its API key from ~/feny-stream/meta_api_key.txt (0600, installed
by Attila at his own terminal — the key never transits chat or Muse's
VM) and optional config from ~/feny-stream/brain.json:
  {"enabled": true, "model": "muse-spark-1.3",
   "reasoning_effort": "minimal", "max_tokens": 400,
   "session_idle_s": 600, "history_turns": 6}

stream_turn(user_text, history, device_state) yields, in true order:
  ("phrase", text)  — spoken-sized chunks of the reply
  ("action", {"name": "set_volume", "args": {"percent": N}})
                    — a self-command the relay executes on the device
The model gets ONE tool: set_volume, for when the user asks Fény to
change her own speaker volume. If it calls the tool, the actions are
yielded as soon as the call completes, the tool result is appended,
and a second streamed call produces the spoken confirmation — so the
relay can apply the new volume BEFORE the confirmation is synthesized.

Raises BrainUnavailable on failure before the first yielded item, so
the caller can fall back to the rule-based reply; mid-stream failures
end the turn gracefully with whatever already played.

The system prompt is operational only: spoken-length formatting plus
embodiment facts (she speaks through a small device; her volume is a
number she can set). NO personality content — that layer is a
separate, explicit decision.
"""
import json
import os
import re
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_PATH = os.path.join(HERE, "meta_api_key.txt")
CFG_PATH = os.path.join(HERE, "brain.json")
API_URL = "https://api.meta.ai/v1/chat/completions"

SYSTEM = ("The user hears your reply spoken aloud by a text-to-speech "
          "voice. Answer in 1 to 3 short, plain sentences. No lists, "
          "no markdown, no URLs, no stage directions.")

EMBODIMENT = (
    "You are speaking through Feny, a small desk device with one "
    "speaker. Your voice comes out of that speaker. Its volume is a "
    "percentage you can change with the set_volume tool whenever the "
    "user asks you to be louder, quieter, or set a level. For a small "
    "change ('a little', 'a bit') adjust by about 5 points; for a big "
    "one ('a lot', 'way up') about 25. Your current volume is {vol} "
    "percent. After changing it, confirm briefly in your reply.")

TOOLS = [{
    "type": "function",
    "function": {
        "name": "set_volume",
        "description": ("Set your own speaker volume, in percent "
                        "(0 is silent, 100 is full). Call this only "
                        "when the user asks you to change how loud "
                        "you are."),
        "parameters": {
            "type": "object",
            "properties": {
                "percent": {"type": "integer", "minimum": 0,
                            "maximum": 100},
            },
            "required": ["percent"],
        },
    },
}]


class BrainUnavailable(Exception):
    pass


def _config():
    cfg = {"enabled": True, "model": "muse-spark-1.3",
           "reasoning_effort": "minimal", "max_tokens": 400}
    try:
        with open(CFG_PATH) as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def _key():
    try:
        with open(KEY_PATH) as f:
            return f.read().strip()
    except Exception:
        return ""


def available():
    return bool(_config().get("enabled")) and bool(_key())


def model_name():
    return _config().get("model", "muse-spark-1.3")


def session_config():
    """(idle_seconds, max_history_turns) for the relay's memory."""
    cfg = _config()
    return (float(cfg.get("session_idle_s", 600)),
            int(cfg.get("history_turns", 6)))


_SENT_END = re.compile(r"[.!?](?=\s|$)")


def _cut_phrases(buf, final=False):
    """Split buf into (phrases, remainder)."""
    out = []
    while True:
        m = _SENT_END.search(buf)
        if m and m.end() >= 30:
            out.append(buf[:m.end()].strip())
            buf = buf[m.end():].lstrip()
            continue
        if len(buf) >= 160:
            cut = buf.rfind(" ", 0, 160)
            if cut >= 40:
                out.append(buf[:cut].strip())
                buf = buf[cut:].lstrip()
                continue
        break
    if final and buf.strip():
        out.append(buf.strip())
        buf = ""
    return [p for p in out if p], buf


def _call_stream(messages, cfg, key, with_tools):
    """One streamed completion. Yields ("phrase", text) as content
    arrives and, at the end, ("tools", [{id, name, args}]) if the
    model called any tools."""
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "max_tokens": int(cfg["max_tokens"]),
        "temperature": 0.7,
        "reasoning_effort": cfg["reasoning_effort"],
        "stream": True,
    }
    if with_tools:
        payload["tools"] = TOOLS
    req = urllib.request.Request(
        API_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key},
        method="POST")
    buf = ""
    tool_acc = {}
    with urllib.request.urlopen(req, timeout=25) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                ev = json.loads(body)
            except Exception:
                continue  # Spark's non-standard SSE event; skip
            delta = (ev.get("choices") or [{}])[0].get("delta") or {}
            piece = delta.get("content")
            if piece:
                buf += piece
                phrases, buf = _cut_phrases(buf)
                for p in phrases:
                    yield ("phrase", p)
            for tc in (delta.get("tool_calls") or []):
                idx = tc.get("index", 0)
                slot = tool_acc.setdefault(
                    idx, {"id": tc.get("id") or "", "name": "",
                          "args_str": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["args_str"] += fn["arguments"]
    phrases, buf = _cut_phrases(buf, final=True)
    for p in phrases:
        yield ("phrase", p)
    calls = []
    for idx in sorted(tool_acc):
        slot = tool_acc[idx]
        if not slot["name"]:
            continue
        try:
            args = json.loads(slot["args_str"] or "{}")
        except Exception:
            args = {}
        calls.append({"id": slot["id"], "name": slot["name"],
                      "args": args})
    if calls:
        yield ("tools", calls)


def stream_turn(user_text, history=None, device_state=None):
    cfg = _config()
    key = _key()
    if not key:
        raise BrainUnavailable("no key")
    vol = 10
    if device_state and isinstance(device_state.get("volume_percent"), int):
        vol = device_state["volume_percent"]
    system = SYSTEM + " " + EMBODIMENT.format(vol=vol)
    msgs = [{"role": "system", "content": system}]
    for m in (history or []):
        if m.get("role") in ("user", "assistant") and m.get("content"):
            msgs.append({"role": m["role"], "content": m["content"]})
    msgs.append({"role": "user", "content": user_text})

    yielded = 0
    tool_calls = []
    try:
        for kind, val in _call_stream(msgs, cfg, key, with_tools=True):
            if kind == "phrase":
                yielded += 1
                yield ("phrase", val)
            elif kind == "tools":
                tool_calls = val
    except Exception as e:
        if yielded == 0 and not tool_calls:
            raise BrainUnavailable(str(e))
        return
    if not tool_calls:
        return

    # The model asked to DO something. Emit the actions first (the
    # relay executes them on the spot), then a second call turns the
    # results into speech — the confirmation can already use the new
    # volume.
    results = []
    for tc in tool_calls:
        yield ("action", {"name": tc["name"], "args": tc["args"]})
        yielded += 1
        if tc["name"] == "set_volume":
            pct = tc["args"].get("percent", vol)
            results.append((tc, f"Your speaker volume is now {pct} percent."))
        else:
            results.append((tc, "Done."))
    msgs2 = list(msgs)
    msgs2.append({
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": tc["id"], "type": "function",
            "function": {"name": tc["name"],
                         "arguments": json.dumps(tc["args"])},
        } for tc in tool_calls],
    })
    for tc, result_text in results:
        msgs2.append({"role": "tool", "tool_call_id": tc["id"],
                      "content": result_text})
    try:
        for kind, val in _call_stream(msgs2, cfg, key, with_tools=False):
            if kind == "phrase":
                yield ("phrase", val)
    except Exception:
        return  # the action already happened; speech is best-effort
