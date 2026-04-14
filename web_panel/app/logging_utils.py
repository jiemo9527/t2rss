import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .time_utils import SHANGHAI_TZ


class ShanghaiFormatter(logging.Formatter):
    """日志时间统一使用上海时区。"""

    def formatTime(self, record, datefmt=None):  # noqa: N802
        dt = datetime.fromtimestamp(record.created, tz=SHANGHAI_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat(timespec="seconds")


def create_logger(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("t2rss_panel")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = ShanghaiFormatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = RotatingFileHandler(
        filename=log_file,
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger
