"""Entrypoint: wire the core engine to the WebSocket transport and serve.

An independent surface, parallel to Telegram — same engine, different transport.
`python -m beaboss.web`. Host/port via WEB_HOST / WEB_PORT (default 127.0.0.1:8765).
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from pathlib import Path

from ..config import Settings
from ..core.engine import Engine
from ..core.store import CoreStore
from ..transports.websocket import WebSocketTransport, serve_forever

log = logging.getLogger("beaboss.web")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    store = CoreStore(settings.state_dir)

    engine = Engine(settings, store)
    transport = WebSocketTransport(store)  # rehydrate threads from the store on restart
    engine.attach_transport(transport)
    engine.rehydrate()  # re-surface unfinished workers after a restart

    host = os.getenv("WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.getenv("WEB_PORT", "8765").strip() or "8765")

    # Safe by default: the web surface has no client auth yet, so refuse a public
    # bind unless the operator explicitly accepts the risk (e.g. behind their own
    # auth/proxy). Localhost is fine for dev and SSH tunnels.
    local = {"127.0.0.1", "localhost", "::1", ""}
    if host not in local and os.getenv("WEB_ALLOW_INSECURE_BIND") != "1":
        raise SystemExit(
            f"Refusing to bind the web surface to {host!r}: it has no client "
            "authentication yet, so a public bind would let anyone drive your "
            "agents. Bind to 127.0.0.1 (the default) and reach it via an SSH "
            "tunnel. If you front it with your own "
            "auth/proxy, set WEB_ALLOW_INSECURE_BIND=1 to override."
        )

    # A required handshake token — with the Origin check in the transport, this is
    # what actually makes the localhost bind a boundary (a malicious web page you
    # visit can otherwise open ws://127.0.0.1 and drive the orchestrator).
    token = os.getenv("WEB_TOKEN", "").strip() or secrets.token_urlsafe(16)
    # Print something the operator can actually open: an absolute file:// URL with
    # the token already in it (set WEB_TOKEN to pin the token across restarts).
    index = Path(__file__).resolve().parents[2] / "web" / "index.html"
    suffix = "" if (host in {"127.0.0.1", "localhost", ""} and port == 8765) \
        else f"&host={host}&port={port}"
    log.info("web UI token: %s", token)
    if index.is_file():
        log.info("open the UI → %s?token=%s%s", index.as_uri(), token, suffix)
    else:
        log.info("open web/index.html?token=%s%s in your browser", token, suffix)

    try:
        asyncio.run(serve_forever(engine, transport, host, port, token))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
