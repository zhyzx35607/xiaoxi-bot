# main.py - QQ Bot entry point
import asyncio
import logging
import os
import sys

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BASE_DIR)

from app.bootstrap import amain
from app.config import apply_env_overrides, load_config, migrate_config
from app.logging_setup import setup_logging

log, chat_log = setup_logging(_BASE_DIR)


def main():
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log.info("Stopped by user")
    except Exception as error:
        log.exception("Fatal: %s", error)
        sys.exit(1)


if __name__ == "__main__":
    main()
