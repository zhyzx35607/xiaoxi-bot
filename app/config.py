"""Compatibility access to configuration loading and migration."""


def apply_env_overrides(config):
    from main import apply_env_overrides as implementation
    return implementation(config)


def load_config(config_path):
    from main import load_config as implementation
    return implementation(config_path)


def migrate_config(config):
    from main import migrate_config as implementation
    return implementation(config)
