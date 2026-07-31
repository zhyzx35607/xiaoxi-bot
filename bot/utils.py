"""Compatibility wrapper for storage utilities."""

from .storage.json_store import atomic_write_json

__all__ = ["atomic_write_json"]
