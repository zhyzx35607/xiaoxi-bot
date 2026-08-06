"""Application logging initialization."""

import logging
from logging.handlers import RotatingFileHandler
import os


def setup_logging(base_dir):
    log_dir = os.getenv("QQBOT_LOG_DIR") or base_dir
    disable_files = os.getenv("QQBOT_DISABLE_FILE_LOG", "").lower() in {"1", "true", "yes", "on"}
    disable_chat = disable_files or os.getenv("QQBOT_DISABLE_CHAT_LOG", "").lower() in {
        "1", "true", "yes", "on",
    }
    if not disable_files:
        os.makedirs(log_dir, exist_ok=True)
    handlers = []
    if not disable_files:
        handlers.append(RotatingFileHandler(
            os.path.join(log_dir, "bot.log"),
            maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
        ))
    if os.getenv("QQBOT_CONSOLE_LOG", "").lower() in {"1", "true", "yes", "on"}:
        handlers.append(logging.StreamHandler())
    if not handlers:
        handlers.append(logging.NullHandler())

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    application_log = logging.getLogger("qqbot")
    chat_log = logging.getLogger("qqbot.chat")
    chat_log.setLevel(logging.INFO)
    chat_log.propagate = False
    if not chat_log.handlers:
        if disable_chat:
            chat_handler = logging.NullHandler()
        else:
            chat_handler = RotatingFileHandler(
                os.path.join(log_dir, "chat.log"),
                maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
            )
            chat_handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
            ))
        chat_log.addHandler(chat_handler)

    for log_path in (os.path.join(log_dir, "bot.log"), os.path.join(log_dir, "chat.log")):
        try:
            os.chmod(log_path, 0o600)
        except OSError:
            pass
    return application_log, chat_log


def get_application_loggers():
    return logging.getLogger("qqbot"), logging.getLogger("qqbot.chat")
