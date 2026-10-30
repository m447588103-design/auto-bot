"""Consistent, non-emoji log formatting for every module."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_CONFIGURED = False


class _ColourFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[35m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        colour = self.COLORS.get(record.levelname, "")
        record.levelname_c = f"{colour}{record.levelname:<8}{self.RESET}"
        return super().format(record)


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    """Configure root logging once, with optional rotating file output."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(
        _ColourFormatter(
            "%(asctime)s | %(levelname_c)s | %(name)-28s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(stream)

    if log_dir is not None:
        try:
            from logging.handlers import RotatingFileHandler

            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_dir / "autobot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(
                logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
            )
            root.addHandler(file_handler)
        except OSError:  # pragma: no cover - read-only fs
            root.warning("Could not create log file handler in %s", log_dir)

    # discord.py is very chatty at DEBUG
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("google").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    _CONFIGURED = True
