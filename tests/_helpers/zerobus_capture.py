"""ZeroBus debug-log sink capture helper for E2E tests."""

from __future__ import annotations

import http.server
import json
import threading


class ZeroBusCapture:
    """Local HTTP endpoint implementing the ZeroBus ingest contract.

    The runner's real debug-log sink mints a token from
    ``<workspace_url>/oidc/v1/token`` and POSTs serialized row batches to the
    insert URL; this server answers both and records every received row, so a
    test can assert on exactly what the sink persisted.
    """

    def __init__(self) -> None:
        self._rows: list[dict[str, object]] = []
        self._lock = threading.Lock()
        rows, lock = self._rows, self._lock

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path.endswith("/oidc/v1/token"):
                    payload = json.dumps({"access_token": "e2e-token", "expires_in": 3600})
                elif "/tables/" in self.path:
                    with lock:
                        rows.extend(json.loads(body))
                    payload = "{}"
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = payload.encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args: object) -> None:
                return

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def insert_url(self) -> str:
        return f"{self.base_url}/zerobus/v1/tables/main.omnigent.debug_logs/insert"

    def rows(self) -> list[dict[str, object]]:
        with self._lock:
            return list(self._rows)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
