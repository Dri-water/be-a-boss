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

    try:
        asyncio.run(serve_forever(engine, transport, host, port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
