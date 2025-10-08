from __future__ import annotations

import logging
import sys
from typing import Optional


def setup_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("zerotts")
    
    # Очищаем все существующие обработчики для избежания дублирования
    logger.handlers.clear()
    
    logger.setLevel(level.upper())

    handler = logging.StreamHandler(sys.stdout)
    fmt = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    handler.setFormatter(logging.Formatter(fmt))
    logger.addHandler(handler)
    
    # Отключаем распространение на корневой логгер
    logger.propagate = False
    
    # Также отключаем корневой логгер для предотвращения дублирования
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.propagate = False
    
    return logger


