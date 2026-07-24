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
    settings = Settings.from_env()
    from ..logsetup import setup_logging
    setup_logging(settings.state_dir)  # console + a rotating file that survives redeploys
    store = CoreStore(settings.state_dir)

    engine = Engine(settings, store)
    transport = WebSocketTransport(store, bot_name=settings.bot_name)  # rehydrate + brand
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
    # The app shell ships inside the package (src/beaboss/web/static), so it resolves
    # the same in a source checkout, a wheel, and the Docker image — and the server
    # serves it over HTTP so the operator opens a real http:// URL (no file://).
    web_dir = Path(__file__).resolve().parent / "static"
    served = web_dir.is_dir()
    display_host = host if host not in {"", "0.0.0.0", "::"} else "127.0.0.1"
    log.info("web UI token: %s", token)
    if served:
        log.info("open the UI → http://%s:%s/?token=%s", display_host, port, token)
    else:  # assets missing (broken build) — don't advertise a URL that will 404
        log.warning("web assets not found at %s — the UI won't be served; "
                    "check the package build", web_dir)

    try:
        asyncio.run(serve_forever(engine, transport, host, port, token,
                                  web_dir if served else None))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
