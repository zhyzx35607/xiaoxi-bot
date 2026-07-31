"""Logging configuration boundary for the current application entrypoint."""

import logging


def get_application_loggers():
    return logging.getLogger("qqbot"), logging.getLogger("qqbot.chat")
