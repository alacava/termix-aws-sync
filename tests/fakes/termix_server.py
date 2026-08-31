"""A tiny in-process fake of the Termix REST API, for the e2e test.

Mirrors the real server's confirmed semantics (see termix.py's module
docstring): PUT is a full replace, not a merge.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

_ID_PATH_RE = re.compile(r"^/host/db/host/(\d+)$")


class FakeTermixServer:
    def __init__(self, hosts: List[Dict[str, Any]]):
        self.hosts: Dict[int, Dict[str, Any]] = {h["id"]: dict(h) for h in hosts}
        self._next_id = max(self.hosts, default=1000) + 1
        self.calls: List[tuple] = []

        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # keep test output quiet

            def _send_json(self, status: int, payload: Any) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_body(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                return json.loads(self.rfile.read(length))

            def do_GET(self):
                if self.path == "/host/db/host":
                    server.calls.append(("GET", self.path, None))
                    self._send_json(200, list(server.hosts.values()))
                    return
                self._send_json(404, {"error": "not found"})

            def do_POST(self):
                if self.path == "/host/db/host":
                    body = self._read_body()
                    server.calls.append(("POST", self.path, body))
                    host_id = server._next_id
                    server._next_id += 1
                    record = dict(body)
                    record["id"] = host_id
                    server.hosts[host_id] = record
                    self._send_json(200, record)
                    return
                self._send_json(404, {"error": "not found"})

            def do_PUT(self):
                m = _ID_PATH_RE.match(self.path)
                if m:
                    host_id = int(m.group(1))
                    body = self._read_body()
                    server.calls.append(("PUT", self.path, body))
                    if host_id not in server.hosts:
                        self._send_json(404, {"error": "not found"})
                        return
                    record = dict(body)  # full replace, matching the real API
                    record["id"] = host_id
                    server.hosts[host_id] = record
                    self._send_json(200, record)
                    return
                self._send_json(404, {"error": "not found"})

            def do_DELETE(self):
                m = _ID_PATH_RE.match(self.path)
                if m:
                    host_id = int(m.group(1))
                    server.calls.append(("DELETE", self.path, None))
                    server.hosts.pop(host_id, None)
                    self._send_json(200, {"message": "deleted"})
                    return
                self._send_json(404, {"error": "not found"})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
