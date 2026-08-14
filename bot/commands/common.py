"""Shared command configuration persistence helpers."""

import json
import os

from ..utils import atomic_write_json

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.getenv("QQBOT_CONFIG_PATH") or os.path.join(_ROOT, "config.json")

def _load():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)

def _save(c):
    atomic_write_json(CONFIG_PATH, c, indent=2)


def _commit(d, cfg):
    """Persist cfg and refresh the in-memory config without dropping env-only secrets."""
    _save(cfg)
    # Local import: app.config already imports bot.utils, so importing it lazily
    # avoids a module-level dependency cycle.
    from app.config import apply_env_overrides
    d.config = apply_env_overrides(cfg)


def resolve_scoped_group_targets(dispatcher, group_id, user_id, args, *,
                                 allow_all=False, require_configured=False):
    """Resolve group targets without allowing a group command to escape its scope."""
    text = str(args or "").strip()
    groups = dispatcher.config.get("groups", {})
    if group_id:
        if text:
            return [], "群聊里只能操作当前群，请不要附加群号"
        target = str(group_id)
        if require_configured and target not in groups:
            return [], "当前群还没有配置，不能执行这个操作"
        return [target], None
    if user_id != dispatcher.config.get("bot_owner"):
        return [], "跨群操作只能由最高主人在私聊中执行"
    if not text:
        return [], "请明确写群号；要操作全部已配置群请写 all"
    if text.lower() == "all":
        if not allow_all:
            return [], "这个操作不支持 all"
        targets = list(groups)
    else:
        values = text.split()
        if not values or any(not value.isdigit() for value in values):
            return [], "群号只能写数字，多个群号用空格分开"
        targets = list(dict.fromkeys(values))
    if len(targets) > 100:
        return [], "一次最多操作 100 个群"
    if require_configured:
        unknown = [value for value in targets if value not in groups]
        if unknown:
            return [], "这些群还没有配置：" + ", ".join(unknown[:5])
    if not targets:
        return [], "还没有配置可操作的群"
    return targets, None
