"""Logging shared by every entrypoint: console PLUS a rotating file in the state
volume. stdout logs vanish when the container is recreated on redeploy, which makes
after-the-fact auditing impossible — the file in state/ survives that."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(state_dir: Path | str, level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format=_FMT)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # every getUpdates poll is INFO
    try:
        state = Path(state_dir)
        state.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            state / "beaboss.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter(_FMT))
        handler.setLevel(level)
        logging.getLogger().addHandler(handler)
        logging.getLogger("beaboss").info("file logging -> %s", state / "beaboss.log")
    except OSError as e:  # never let logging setup crash the boot
        logging.warning("could not set up file logging in %s: %s", state_dir, e)
