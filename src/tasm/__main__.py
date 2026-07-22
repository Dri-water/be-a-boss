"""Entrypoint: load config, wire the core engine + Telegram transport, poll."""

from __future__ import annotations

import logging

from .config import Settings
from .core.store import CoreStore
from .transports.telegram import build_application


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # httpx logs every getUpdates poll at INFO — quiet it.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = Settings.from_env()
    store = CoreStore(settings.state_dir)
    app = build_application(settings, store)

    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
