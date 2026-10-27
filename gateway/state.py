#!/usr/bin/env python3
"""mabel-world state layer (rung 3).

Turns office-01's raw 10-second light observations into statements --
a diary Feny can be asked about:

    "9:41 PM -- office-01: the office went dark (als 35 -> 8)"

Reads observations-YYYY-MM-DD.jsonl (written by gateway/listener.py),
runs the stream through a small state machine (light bands with a
60-second dwell so flicker doesn't spam the diary, plus node liveness),
and appends statement records to diary-YYYY-MM-DD.jsonl.

Modes:
  --backfill   process every observation file once, write the diary,
               and save a checkpoint at the live edge
  --follow     resume from the checkpoint and keep appending live
               (this is what the systemd service runs)
  --today      print today's statements, human-readable

State lives in state-checkpoint.json next to this file. Re-running
--backfill rebuilds the diary from scratch; --follow never re-emits
a statement the checkpoint has already passed.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DIR = Path(__file__).resolve().parent
LOCAL = ZoneInfo("America/New_York")
DWELL_SAMPLES = 6          # 6 x 10 s: a new band must hold ~60 s to count
QUIET_AFTER_S = 90         # no observation for 90 s -> node went quiet

BANDS = ["dark", "dim", "lit", "bright"]


def band_of(als):
    if als < 12:
        return "dark"
    if als < 60:
        return "dim"
    if als < 2000:
        return "lit"
    return "bright"


VERBS = {
    ("dim", "dark"): "the office went dark",
    ("lit", "dark"): "the office went dark",
    ("bright", "dark"): "the office went dark",
    ("lit", "dim"): "the office dimmed",
    ("bright", "dim"): "the office dimmed",
    ("dark", "dim"): "the office lightened a little, still dim",
    ("bright", "lit"): "the bright light faded back to normal",
    ("dark", "lit"): "the lights came up in the office",
    ("dim", "lit"): "the lights came up in the office",
    ("dark", "bright"): "a bright light hit the office sensor",
    ("dim", "bright"): "a bright light hit the office sensor",
    ("lit", "bright"): "a bright light hit the office sensor",
}


def parse_ts(rec):
    s = rec.get("gw_received_at")
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None


class StateMachine:
    def __init__(self, state=None):
        s = state or {}
        self.band = s.get("band")              # settled band, None until first sample
        self.pending = s.get("pending")        # candidate band
        self.pending_count = s.get("pending_count", 0)
        self.pending_sum = s.get("pending_sum", 0)
        self.settled_sum = s.get("settled_sum", 0)
        self.settled_count = s.get("settled_count", 0)
        self.last_seen = s.get("last_seen")    # epoch seconds
        self.quiet = s.get("quiet", False)

    def to_state(self):
        return {
            "band": self.band, "pending": self.pending,
            "pending_count": self.pending_count, "pending_sum": self.pending_sum,
            "settled_sum": self.settled_sum, "settled_count": self.settled_count,
            "last_seen": self.last_seen, "quiet": self.quiet,
        }

    def feed(self, ts, als):
        """Feed one observation; return a statement dict or None."""
        epoch = ts.timestamp()
        stmt = None
        if self.last_seen is not None and epoch - self.last_seen > QUIET_AFTER_S:
            if not self.quiet:
                self.quiet = True
                stmt = self._statement(ts, "liveness", "went_quiet",
                                       "office-01 went quiet (no observations)")
            # a gap also resets any pending transition
            self.pending, self.pending_count, self.pending_sum = None, 0, 0
        if self.quiet and stmt is None:
            self.quiet = False
            stmt = self._statement(ts, "liveness", "came_back",
                                   "office-01 is back, reporting again")
        self.last_seen = epoch

        b = band_of(als)
        if self.band is None:
            self.band = b
            self.settled_sum, self.settled_count = als, 1
            return stmt
        if b == self.band:
            self.pending, self.pending_count, self.pending_sum = None, 0, 0
            self.settled_sum += als
            self.settled_count += 1
            return stmt
        # candidate transition
        if self.pending == b:
            self.pending_count += 1
            self.pending_sum += als
        else:
            self.pending, self.pending_count, self.pending_sum = b, 1, als
        if self.pending_count >= DWELL_SAMPLES:
            prev_avg = round(self.settled_sum / max(self.settled_count, 1))
            new_avg = round(self.pending_sum / self.pending_count)
            old, self.band = self.band, b
            self.settled_sum, self.settled_count = self.pending_sum, self.pending_count
            self.pending, self.pending_count, self.pending_sum = None, 0, 0
            text = VERBS.get((old, b), f"the office light changed ({old} -> {b})")
            return self._statement(ts, "light", f"{old}_to_{b}",
                                   f"{text} (als {prev_avg} -> {new_avg})")
        return stmt

    @staticmethod
    def _statement(ts, kind, event, text):
        local = ts.astimezone(LOCAL)
        return {
            "ts": local.isoformat(timespec="seconds"),
            "node": "office-01",
            "kind": kind,
            "event": event,
            "statement": f"{local.strftime('%-I:%M %p')} -- office-01: {text}",
        }


def obs_files():
    return sorted(DIR.glob("observations-*.jsonl"))


def diary_path_for(stmt):
    day = stmt["ts"][:10]
    return DIR / f"diary-{day}.jsonl"


def append_stmt(stmt, written):
    p = diary_path_for(stmt)
    with open(p, "a") as f:
        f.write(json.dumps(stmt) + "\n")
    written.append(stmt)


def iter_records(path, offset=0):
    with open(path, "rb") as f:
        f.seek(offset)
        for line in f:
            yield line
        return f.tell()


def backfill():
    # rebuild diary from scratch
    for p in DIR.glob("diary-*.jsonl"):
        p.unlink()
    sm = StateMachine()
    written = []
    last_file, last_offset = None, 0
    for path in obs_files():
        offset = 0
        with open(path, "rb") as f:
            for raw in f:
                offset += len(raw)
                try:
                    rec = json.loads(raw)
                except Exception:
                    continue
                if rec.get("type") != "observation" or "als_raw" not in rec:
                    continue
                ts = parse_ts(rec)
                if ts is None:
                    continue
                stmt = sm.feed(ts, rec["als_raw"])
                if stmt:
                    append_stmt(stmt, written)
        last_file, last_offset = path.name, offset
    ckpt = {"file": last_file, "offset": last_offset, "machine": sm.to_state()}
    (DIR / "state-checkpoint.json").write_text(json.dumps(ckpt))
    print(f"backfill: {len(written)} statements written")
    for s in written:
        print(" ", s["statement"])


def follow():
    ckpt_path = DIR / "state-checkpoint.json"
    if not ckpt_path.exists():
        backfill()
    ckpt = json.loads(ckpt_path.read_text())
    sm = StateMachine(ckpt.get("machine"))
    cur_file, offset = ckpt.get("file"), ckpt.get("offset", 0)
    print(f"state layer following {cur_file}@{offset}", flush=True)
    while True:
        files = obs_files()
        if files:
            newest = files[-1].name
            if cur_file is None or newest != cur_file and newest > cur_file:
                cur_file, offset = newest, 0
        path = DIR / cur_file if cur_file else None
        if path and path.exists():
            with open(path, "rb") as f:
                f.seek(offset)
                lines = f.readlines()
                offset = f.tell()
            for raw in lines:
                try:
                    rec = json.loads(raw)
                except Exception:
                    continue
                if rec.get("type") != "observation" or "als_raw" not in rec:
                    continue
                ts = parse_ts(rec)
                if ts is None:
                    continue
                stmt = sm.feed(ts, rec["als_raw"])
                if stmt:
                    sink = []
                    append_stmt(stmt, sink)
                    print(stmt["statement"], flush=True)
            ckpt = {"file": cur_file, "offset": offset, "machine": sm.to_state()}
            ckpt_path.write_text(json.dumps(ckpt))
        time.sleep(5)


def today():
    day = datetime.now(LOCAL).strftime("%Y-%m-%d")
    p = DIR / f"diary-{day}.jsonl"
    if not p.exists():
        print("no statements yet today")
        return
    for line in open(p):
        try:
            print(json.loads(line)["statement"])
        except Exception:
            pass


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "--follow"
    if mode == "--backfill":
        backfill()
    elif mode == "--today":
        today()
    else:
        follow()
