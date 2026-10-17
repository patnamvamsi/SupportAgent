"""fake-sn — a ServiceNow stand-in for local compose (10-wp WP-000).

The real ``FakeServiceNow`` port implementation lands with WP-008; this is
the compose service that keeps the walking-skeleton network shape honest.

Endpoints (URL shapes per 02 §interfaces, e.g. /api/now/table/{table}):
  GET /health                → {"service": "fake-sn", "status": "ok"}
  GET /api/now/table/{table} → {"result": [], "total": 0, "sys_version": "fake"}
  anything else              → 404 {"error": "not found"}

Stdlib only; no dependencies, no network egress.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


class FakeSNHandler(BaseHTTPRequestHandler):
    server_version = "fake-sn/0.0"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, {"service": "fake-sn", "status": "ok"})
        elif path.startswith("/api/now/table/"):
            # GlideRecord response shape (empty result set), per 02 §interfaces.
            self._send(200, {"result": [], "total": 0, "sys_version": "fake"})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep compose logs quiet; behaviour is asserted by the client, not here.
        pass


def main() -> None:
    ThreadingHTTPServer(("0.0.0.0", 8081), FakeSNHandler).serve_forever()


if __name__ == "__main__":
    main()
