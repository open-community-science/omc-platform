"""Minimal static file server for the danaSeq viz SPA."""

import os
from http.server import HTTPServer, SimpleHTTPRequestHandler

VIZ_DIR = os.environ.get("VIZ_DIR", "/app/viz")
VIZ_PORT = int(os.environ.get("VIZ_PORT", "8082"))
VIZ_ROOT = os.environ.get("VIZ_ROOT_PATH", "")  # e.g. "/session-proxy/9108"


class VizHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=VIZ_DIR, **kwargs)

    def translate_path(self, path):
        # Strip the reverse-proxy prefix so /session-proxy/9108/foo → /foo
        if VIZ_ROOT and path.startswith(VIZ_ROOT):
            path = path[len(VIZ_ROOT):] or "/"
        return super().translate_path(path)

    def log_message(self, format, *args):
        pass  # silence request logs


if __name__ == "__main__":
    if not os.path.exists(os.path.join(VIZ_DIR, "index.html")):
        # Serve a placeholder if the SPA isn't installed
        os.makedirs(VIZ_DIR, exist_ok=True)
        with open(os.path.join(VIZ_DIR, "index.html"), "w") as f:
            f.write("<html><body style='font-family:system-ui;color:#888;display:flex;"
                    "align-items:center;justify-content:center;height:100vh;margin:0'>"
                    "<p>Data Explorer not available — no viz app installed.</p>"
                    "</body></html>")
    server = HTTPServer(("0.0.0.0", VIZ_PORT), VizHandler)
    print(f"Viz server on :{VIZ_PORT} serving {VIZ_DIR} (root: {VIZ_ROOT or '/'})")
    server.serve_forever()
