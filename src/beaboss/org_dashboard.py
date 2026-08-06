"""Tiny read-only HTTP surface for the durable organization snapshot.

It intentionally does not construct an Engine or a transport.  Telegram remains
the sole live transport while browsers and editor extensions observe the atomic
``organization.json`` projection written by the engine.
"""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


_STATIC = Path(__file__).parent / "web" / "static" / "org-dashboard.html"


class OrganizationHandler(BaseHTTPRequestHandler):
    server_version = "be-a-boss-org/1"

    @property
    def state_path(self) -> Path:
        return Path(self.server.state_dir) / "organization.json"  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        # A localhost bind is not sufficient against DNS rebinding: a hostile
        # hostname can resolve to loopback while retaining its attacker-controlled
        # Host header. Only the local names this observer is designed for are valid.
        hostname = urlsplit("//" + (self.headers.get("Host") or "")).hostname
        if (hostname or "").casefold() not in {"localhost", "127.0.0.1", "::1"}:
            self._bytes(
                HTTPStatus.MISDIRECTED_REQUEST, b"invalid host\n", "text/plain")
            return
        path = self.path.partition("?")[0]
        if path in ("/", "/index.html"):
            self._file(_STATIC, "text/html; charset=utf-8", no_store=False)
        elif path == "/organization.json":
            self._file(self.state_path, "application/json; charset=utf-8")
        elif path == "/healthz":
            self._bytes(HTTPStatus.OK, b'{"ok":true}\n', "application/json")
        else:
            self._bytes(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain")

    def _file(self, path: Path, content_type: str, *, no_store: bool = True) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._bytes(
                HTTPStatus.SERVICE_UNAVAILABLE,
                b'{"error":"organization snapshot is not available yet"}\n',
                "application/json",
            )
            return
        self._bytes(HTTPStatus.OK, body, content_type, no_store=no_store)

    def _bytes(
        self, status: HTTPStatus, body: bytes, content_type: str, *, no_store: bool = True,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'none'",
        )
        if no_store:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        # Polling organization.json every two seconds should not flood container logs.
        if self.path.partition("?")[0] != "/organization.json":
            super().log_message(format, *args)


def main() -> None:
    host = os.getenv("ORG_DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("ORG_DASHBOARD_PORT", "8766"))
    state_dir = os.getenv("STATE_DIR", "state")
    server = ThreadingHTTPServer((host, port), OrganizationHandler)
    server.state_dir = state_dir  # type: ignore[attr-defined]
    print(f"organization dashboard listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
