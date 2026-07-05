import logging
import os
from pathlib import Path

# Handler-less at import: modules can `from src.logging_setup import logger`
# freely, and nothing touches the filesystem until setup_logging() runs.
logger = logging.getLogger("fez_collector")


def setup_logging() -> logging.Logger:
    """Attach console and file handlers to the root logger.

    Called once from main() — never at import time, so importing the codebase
    (tests, tooling) never creates log directories or files.
    """
    log_dir = Path(os.getenv("FEZ_LOG_DIR", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)

    log_format = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(log_format)
    root_logger.addHandler(console_handler)

    file_handler = logging.FileHandler(log_dir / "fez_collector.log")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(log_format)
    root_logger.addHandler(file_handler)

    error_handler = logging.FileHandler(log_dir / "errors.log")
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(log_format)
    root_logger.addHandler(error_handler)

    return logger
