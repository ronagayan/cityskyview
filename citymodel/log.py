"""One logger for the whole package; the UI attaches its own handler to it."""

from __future__ import annotations

import logging
import logging.handlers

from .paths import log_file

logger = logging.getLogger("citymodel")


def setup_logging(level: int = logging.INFO, console: bool = True) -> logging.Logger:
    """Rotating file handler (+ optional console). Idempotent."""
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    if not any(isinstance(h, logging.handlers.RotatingFileHandler)
               for h in logger.handlers):
        try:
            fh = logging.handlers.RotatingFileHandler(
                log_file(), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            fh.setFormatter(fmt)
            fh.setLevel(logging.DEBUG)
            logger.addHandler(fh)
        except OSError:
            pass
    if console and not any(type(h) is logging.StreamHandler for h in logger.handlers):
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        ch.setLevel(level)
        logger.addHandler(ch)
    return logger
