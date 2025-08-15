import json
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import config
from database import db


class CIRequestHandler(BaseHTTPRequestHandler):
    server_version = "CIHooksHTTP/1.0"

    def _unauthorized(self):
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": "unauthorized"}).encode("utf-8"))

    def _bad_request(self, message="bad request"):
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": message}).encode("utf-8"))

    def _ok(self, payload):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def do_POST(self):
        # Auth
        token = self.headers.get("X-Deploy-Token")
        if not config.CI_SHARED_SECRET or token != config.CI_SHARED_SECRET:
            return self._unauthorized()

        length = int(self.headers.get("Content-Length", "0"))
        try:
            raw_body = self.rfile.read(length) if length > 0 else b"{}"
            body = json.loads(raw_body.decode("utf-8") or "{}")
        except Exception:
            return self._bad_request("invalid json")

        path = urlparse(self.path).path
        if path == "/deploy/start":
            minutes = body.get("minutes")
            service_ids = body.get("service_ids")
            if not isinstance(minutes, int) or minutes <= 0:
                return self._bad_request("minutes must be positive int")
            if service_ids is None:
                service_ids = list(config.SERVICES_TO_MONITOR)
            if isinstance(service_ids, str):
                service_ids = [service_ids]
            db.start_deploy_window(service_ids, minutes)
            return self._ok({"status": "ok", "applied_to": len(service_ids), "minutes": minutes})
        elif path == "/deploy/end":
            service_ids = body.get("service_ids")
            if service_ids is None:
                service_ids = list(config.SERVICES_TO_MONITOR)
            if isinstance(service_ids, str):
                service_ids = [service_ids]
            db.end_deploy_window(service_ids)
            return self._ok({"status": "ok", "applied_to": len(service_ids)})
        else:
            return self._bad_request("unknown endpoint")

    # Mute noisy logs
    def log_message(self, format, *args):
        return


def run_server_background():
    if not config.ENABLE_CI_HTTP_HOOKS:
        return None
    if not config.CI_SHARED_SECRET:
        print("⚠️ ENABLE_CI_HTTP_HOOKS=true אבל CI_SHARED_SECRET לא הוגדר. השרת לא יופעל.")
        return None
    server = ThreadingHTTPServer(("0.0.0.0", config.CI_HTTP_PORT), CIRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"🔗 CI hooks HTTP server listening on :{config.CI_HTTP_PORT}")
    return server