"""结构化日志配置。

参考 AShareGateway 的 FileHandler + Formatter 风格，改为控制台 + 轮转文件双输出，
并支持热重载时去重 handler。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Settings

_FMT = logging.Formatter("%(asctime)s [%(name)s][%(levelname)s] %(message)s")


def configure_logging(settings: Settings) -> logging.Logger:
    """配置根 logger，返回应用主 logger。"""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler()
    stream.setFormatter(_FMT)
    stream.setLevel(level)
    root.addHandler(stream)

    log_dir = Path(settings.log_folder)
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "twilightcup.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(_FMT)
    file_handler.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

    return logging.getLogger("twilightcup")
