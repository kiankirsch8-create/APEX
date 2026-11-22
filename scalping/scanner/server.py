"""Tiny local web server: the dashboard page plus /state.json (stdlib only)."""
import json, os, threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(__file__)
STATE = {"updated": None, "rows": [], "status": "starting"}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/state.json"):
            body = json.dumps(STATE, default=str).encode(); ctype = "application/json"
        elif self.path in ("/", "/index.html"):
            body = open(os.path.join(HERE, "dashboard.html"), "rb").read(); ctype = "text/html; charset=utf-8"
        else:
            self.send_error(404); return
        self.send_response(200); self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


def start(port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
