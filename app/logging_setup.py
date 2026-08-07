"""Application logging initialization."""

import logging
import os
import re
from logging.handlers import RotatingFileHandler


_HEADER_SECRET_PATTERN = re.compile(
    r"(?i)([\"']?(?:authorization|cookie|set-cookie)[\"']?\s*[:=]\s*[\"']?)"
    r"(?:bearer\s+)?([^\"'\r\n,}]+)"
)
_SECRET_PATTERN = re.compile(
    r"(?i)([\"']?(?:access_?token|token|password|passkey|api[-_]?key|x[-_]?api[-_]?key|client[-_]?key|sessdata)"
    r"[\"']?\s*[:=]\s*[\"']?)([^\"'\s,&}#?]+)"
)
_URL_QUERY_SECRET_PATTERN = re.compile(
    r"(?i)([?&#](?:access_token|token|password|passkey|api[-_]?key|x[-_]?api[-_]?key|"
    r"client[-_]?key|authorization|cookie|sessdata)(?:=[^&#\s]*)?)"
)
_LONG_ID_PATTERN = re.compile(r"(?<!\d)\d{5,12}(?!\d)")
_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PAYLOAD_PATTERN = re.compile(
    r"(?i)(\[CQ:|(?:raw_message|message|content|text|reply|summary|query|body|payload)\s*[:=])"
)


def sanitize_log_message(message, limit=4000):
    text = _CONTROL_PATTERN.sub("", str(message).replace("\r", ""))
    text = _URL_QUERY_SECRET_PATTERN.sub(
        lambda match: match.group(0).split("=", 1)[0] + "=<redacted>"
        if "=" in match.group(0) else match.group(0),
        text,
    )
    text = _HEADER_SECRET_PATTERN.sub(r"\1<redacted>", text)
    text = _SECRET_PATTERN.sub(r"\1<redacted>", text)
    text = _LONG_ID_PATTERN.sub("<id>", text)
    payload = _PAYLOAD_PATTERN.search(text)
    if payload:
        text = text[:payload.start()].rstrip() + " <payload redacted>"
    if len(text) > limit:
        text = text[:limit] + "...<truncated>"
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return sanitize_log_message(super().format(record))


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

    application_formatter = RedactingFormatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    for handler in handlers:
        handler.setFormatter(application_formatter)

    logging.basicConfig(
        level=logging.INFO,
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
            chat_handler.setFormatter(RedactingFormatter(
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
