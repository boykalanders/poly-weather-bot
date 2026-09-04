"""Logging configuration: console + rotating file."""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .config import ROOT, settings

FMT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"


def setup(level: str | None = None) -> None:
    # Market labels carry degree signs, em-dashes and emoji. The default Windows
    # console codepage (cp1252) raises UnicodeEncodeError on those, which would
    # kill a log call mid-trade, so force UTF-8 on the streams we own.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, (level or settings.log_level).upper(), logging.INFO))
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(FMT))
    root.addHandler(console)

    fileh = logging.handlers.RotatingFileHandler(
        Path(log_dir) / "bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    fileh.setFormatter(logging.Formatter(FMT))
    root.addHandler(fileh)

    # These libraries are chatty at INFO and drown out our own logs.
    for noisy in ("httpx", "httpcore", "telegram.ext.Application", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
