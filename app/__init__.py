"""Application configuration, logging, and bootstrap boundaries."""

__all__ = [
    "amain",
    "apply_env_overrides",
    "get_application_loggers",
    "load_config",
    "migrate_config",
    "setup_logging",
]


def __getattr__(name):
    if name == "amain":
        from .bootstrap import amain
        return amain
    if name in {"apply_env_overrides", "load_config", "migrate_config"}:
        from . import config
        return getattr(config, name)
    if name in {"get_application_loggers", "setup_logging"}:
        from . import logging_setup
        return getattr(logging_setup, name)
    raise AttributeError(name)
