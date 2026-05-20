import os
os.environ["AUTO_CAST_FOR_DYNACONF"] = "false"
import json
import logging
import sys
from enum import Enum

from loguru import logger

from pr_agent.config_loader import get_settings


class LoggingFormat(str, Enum):
    CONSOLE = "CONSOLE"
    JSON = "JSON"


def json_format(record: dict) -> str:
    return record["message"]


def analytics_filter(record: dict) -> bool:
    return record.get("extra", {}).get("analytics", False)


def inv_analytics_filter(record: dict) -> bool:
    return not record.get("extra", {}).get("analytics", False)


def setup_logger(level: str = "INFO", fmt: LoggingFormat = LoggingFormat.CONSOLE):
    level: int = logging.getLevelName(level.upper())
    if type(level) is not int:
        level = logging.INFO

    if fmt == LoggingFormat.JSON and os.getenv("LOG_SANE", "0").lower() == "0":  # better debugging github_app
        logger.remove(None)
        logger.add(
            sys.stdout,
            filter=inv_analytics_filter,
            level=level,
            format="{message}",
            colorize=False,
            serialize=True,
        )
    elif fmt == LoggingFormat.CONSOLE: # does not print the 'extra' fields
        logger.remove(None)
        logger.add(sys.stdout, level=level, colorize=True, filter=inv_analytics_filter)

    # Add file logging if LOG_FILE is configured
    log_file_path = get_settings().get("CONFIG.LOG_FILE", "") or os.getenv("LOG_FILE", "")
    if log_file_path:
        # Create directory if it doesn't exist
        log_dir = os.path.dirname(log_file_path)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
        
        logger.add(
            log_file_path,
            level=level,
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
            colorize=False,
            rotation="10 MB",  # Rotate when file reaches 10 MB
            retention="7 days",  # Keep logs for 7 days
            compression="zip",  # Compress rotated files
            enqueue=True,  # Thread-safe logging
        )

    log_folder = get_settings().get("CONFIG.ANALYTICS_FOLDER", "")
    if log_folder:
        pid = os.getpid()
        log_file = os.path.join(log_folder, f"pr-agent.{pid}.log")
        logger.add(
            log_file,
            filter=analytics_filter,
            level=level,
            format="{message}",
            colorize=False,
            serialize=True,
        )

    return logger


def get_logger(*args, **kwargs):
    return logger
