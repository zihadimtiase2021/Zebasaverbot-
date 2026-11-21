import logging
import sys
from logging.handlers import RotatingFileHandler

from unified_core.config import settings


def setup_logging() -> None:
    settings.ensure_dirs()
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(settings.LOG_LEVEL.upper())
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_h = RotatingFileHandler(
        settings.log_dir / "zeba.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_h.setFormatter(fmt)
    root.addHandler(file_h)

    for name in ("telethon", "httpx", "httpcore", "openai", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)
