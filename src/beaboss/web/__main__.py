"""Entrypoint: wire the core engine to the WebSocket transport and serve.

An independent surface, parallel to Telegram — same engine, different transport.
`python -m beaboss.web`. Host/port via WEB_HOST / WEB_PORT (default 127.0.0.1:8765).
"""

from __future__ import annotations

import asyncio
import logging
import os

from ..config import Settings
from ..core.engine import Engine
from ..core.store import CoreStore
from ..transports.websocket import WebSocketTransport, serve_forever


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    store = CoreStore(settings.state_dir)

    engine = Engine(settings, store)
    transport = WebSocketTransport()
    engine.attach_transport(transport)

    host = os.getenv("WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.getenv("WEB_PORT", "8765").strip() or "8765")

    # Safe by default: the web surface has no client auth yet, so refuse a public
    # bind unless the operator explicitly accepts the risk (e.g. behind their own
    # auth/proxy). Localhost is fine for dev, SSH tunnels, and the VS Code extension.
    local = {"127.0.0.1", "localhost", "::1", ""}
    if host not in local and os.getenv("WEB_ALLOW_INSECURE_BIND") != "1":
        raise SystemExit(
            f"Refusing to bind the web surface to {host!r}: it has no client "
            "authentication yet, so a public bind would let anyone drive your "
            "agents. Bind to 127.0.0.1 (the default) and reach it via an SSH "
            "tunnel or the VS Code extension. If you front it with your own "
            "auth/proxy, set WEB_ALLOW_INSECURE_BIND=1 to override."
        )

    try:
        asyncio.run(serve_forever(engine, transport, host, port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
