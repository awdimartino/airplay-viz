"""Tiny settings web server (stdlib only), run in a background thread.

GET  /                  the settings page (webui.html)
GET  /api/state         all sections: settings, current values, preset names
POST /api/settings      {"section": id, "values": {key: value}}  apply + save
POST /api/reset         {"section": id}                         back to defaults
POST /api/presets/save  {"section": id, "name": str}            save current values
POST /api/presets/load  {"section": id, "name": str}            apply a preset
POST /api/presets/delete {"section": id, "name": str}

No authentication: meant for a home LAN only.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import settings

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui.html")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):   # keep the journal quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(PAGE, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._send(200, settings.state())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(min(length, 65536)) or b"{}")
            sec = body.get("section", "")
            if self.path == "/api/settings":
                changed = settings.apply(sec, body.get("values", {}))
                msg = f"Applied {len(changed)} change(s)" if changed else "No changes"
            elif self.path == "/api/reset":
                settings.reset(sec)
                msg = "Reset to defaults"
            elif self.path == "/api/presets/save":
                settings.save_preset(sec, body.get("name", ""))
                msg = f"Saved preset “{body['name'].strip()}”"
            elif self.path == "/api/presets/load":
                settings.load_preset(sec, body.get("name", ""))
                msg = f"Loaded preset “{body['name']}”"
            elif self.path == "/api/presets/delete":
                settings.delete_preset(sec, body.get("name", ""))
                msg = f"Deleted preset “{body['name']}”"
            else:
                return self._send(404, {"error": "not found"})
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            return self._send(400, {"error": str(e)})
        self._send(200, {"message": msg, "state": settings.state()})


def start(port):
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"settings page on http://0.0.0.0:{port}/", flush=True)
    return server
