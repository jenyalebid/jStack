"""Localhost control API — stdlib ThreadingHTTPServer, 127.0.0.1 bind only.

No auth (loopback bind is the boundary; the dashboard proxies it).
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer

from . import config


class _Server(ThreadingHTTPServer):
    def server_bind(self):
        # http.server's server_bind calls socket.getfqdn(), a reverse-DNS
        # lookup that hangs for minutes on hosts with no PTR record — the
        # daemon never reaches serve_forever. Loopback-only server: skip it.
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.socket.getsockname()[1]

_RUN_NOW_RE = re.compile(r"^/jobs/([^/]+)/run-now$")
_KILL_RE = re.compile(r"^/runs/([^/]+)/kill$")


def make_server(engine, port: "int|None" = None) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet — daemon stdout is the log
            pass

        def _send(self, code: int, payload) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._send(200, engine.health())
            elif path == "/jobs":
                self._send(200, engine.jobs_view())
            elif path == "/runs":
                self._send(200, engine.runs_view())
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            m = _RUN_NOW_RE.match(path)
            if m:
                ok = engine.run_now(m.group(1))
                self._send(200 if ok else 404, {"ok": ok})
                return
            m = _KILL_RE.match(path)
            if m:
                self._send(200, {"killed": engine.kill_run(m.group(1))})
                return
            if path == "/reload":
                engine.reload()
                self._send(200, {"ok": True})
                return
            self._send(404, {"error": "not found"})

    return _Server(
        ("127.0.0.1", port if port is not None else config.API_PORT), Handler
    )
