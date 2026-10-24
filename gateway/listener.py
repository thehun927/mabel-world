import json, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
DIR = Path.home() / "mabel-world"
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        try:
            rec = json.loads(body)
        except Exception:
            rec = {"raw": body.decode("utf-8", "replace")}
        rec["gw_received_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        fn = DIR / ("observations-%s.jsonl" % time.strftime("%Y-%m-%d"))
        with open(fn, "a") as f:
            f.write(json.dumps(rec) + "\n")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{\"ok\":true}")
    def log_message(self, *a):
        pass
HTTPServer(("0.0.0.0", 8891), H).serve_forever()
