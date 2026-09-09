from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from .config import Config


def setup_logging(config: Config) -> logging.Logger:
    log_file = config.resolve_path(config.get("feedback", "log_file", default="logs/app.log"))
    log_file.parent.mkdir(parents=True, exist_ok=True)
    level_name = config.get("feedback", "log_level", default="INFO")
    level = getattr(logging, str(level_name).upper(), logging.INFO)

    logger = logging.getLogger("dota_voice")
    logger.setLevel(level)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    return logger
