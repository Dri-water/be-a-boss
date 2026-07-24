"""Entrypoint: load config, wire the core engine + Telegram transport, poll."""

from __future__ import annotations

from .config import Settings
from .core.store import CoreStore
from .logsetup import setup_logging
from .transports.telegram import build_application


def main() -> None:
    settings = Settings.from_env()
    setup_logging(settings.state_dir)  # console + a rotating file that survives redeploys
    store = CoreStore(settings.state_dir)
    app = build_application(settings, store)

    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
