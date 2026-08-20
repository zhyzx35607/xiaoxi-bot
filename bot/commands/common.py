"""Shared command configuration persistence helpers."""

import json
import logging
import os
import re

from ..permission import _CONFIG_WRITE_LOCK
from ..utils import atomic_write_json

log = logging.getLogger("qqbot")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.getenv("QQBOT_CONFIG_PATH") or os.path.join(_ROOT, "config.json")

def _load():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)

def _save(c):
    atomic_write_json(CONFIG_PATH, c, indent=2)


def _commit(d, cfg):
    """Persist cfg and refresh the in-memory config without dropping env-only secrets."""
    # Local import: app.config already imports bot.utils, so importing it lazily
    # avoids a module-level dependency cycle.
    from app.config import apply_env_overrides
    with _CONFIG_WRITE_LOCK:
        _save(cfg)
        refreshed = apply_env_overrides(cfg)
        # Update the existing dict in place: AgentRuntime and RoleplayService
        # keep a reference to the original config object and must observe the
        # refreshed values instead of a stale snapshot.
        d.config.clear()
        d.config.update(refreshed)


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


_CQ_AT_RE = re.compile(r"\[CQ:at,qq=(\d+)\]")
_BARE_QQ_RE = re.compile(r"\b\d{5,12}\b")


def split_action_args(args, actions, default=""):
    """Split a leading action word, tolerating targets glued to it.

    Accepts "add @x", "add@x" (CQ code) and "add123" alike. Returns
    (action, rest); when no known action matches, returns (default, args).
    """
    text = str(args or "").lstrip()
    lowered = text.lower()
    for action in sorted(actions, key=len, reverse=True):
        if lowered.startswith(action):
            rest = text[len(action):]
            if not rest or not rest[0].isalpha():
                return action, rest
    return default, text


def parse_target_qqs(args, mentions=None):
    """Parse user targets from @mentions and bare QQ numbers, in any mix.

    Handles multiple targets and targets glued to other text without spaces
    ("add@xxx", "add 123 456", "add@a@b"). Returns (targets, rest_text):
    ordered unique int QQ ids, and the argument text with CQ codes and
    consumed numbers stripped (for parsing trailing parameters like a ban
    duration).
    """
    text = str(args or "")
    targets = []
    for qq in (mentions or []):
        try:
            targets.append(int(qq))
        except (TypeError, ValueError):
            continue
    if not targets:
        # String-form args (private routing) carry @s as CQ codes.
        targets.extend(int(qq) for qq in _CQ_AT_RE.findall(text))
    text = _CQ_AT_RE.sub(" ", text)
    # A number directly followed by a duration unit (分钟/分/min/m) is a
    # parameter, not a QQ target.
    numbers = []
    for m in _BARE_QQ_RE.finditer(text):
        if re.match(r"\s*(?:分钟|分|mins?|m)\b", text[m.end():], re.IGNORECASE):
            continue
        numbers.append(m.group(0))
    targets.extend(int(n) for n in numbers)
    targets = list(dict.fromkeys(targets))
    rest = text
    for n in numbers:
        rest = re.sub(r"(?<!\d)" + re.escape(n) + r"(?!\d)", " ", rest, count=1)
    return targets, rest.strip()


async def format_user_label(d, group_id, qq):
    """Render a user as '名字(QQ号)', falling back to the bare QQ number."""
    try:
        qq = int(qq)
    except (TypeError, ValueError):
        return str(qq)
    name = ""
    try:
        if group_id:
            r = await d.client.get_group_member_info(int(group_id), qq)
            if r.get("status") == "ok":
                data = r.get("data") or {}
                name = data.get("card") or data.get("nickname") or ""
        if not name:
            r = await d.client.get_stranger_info(qq)
            if r.get("status") == "ok":
                name = (r.get("data") or {}).get("nickname") or ""
    except Exception as error:
        log.debug("user label lookup failed for %s: %s", qq, error)
    return "{}({})".format(name, qq) if name else str(qq)


async def format_user_labels(d, group_id, qqs, *, sep="、"):
    labels = []
    for qq in qqs:
        labels.append(await format_user_label(d, group_id, qq))
    return sep.join(labels)


async def format_group_label(d, group_id):
    """Render a group as '群名(群号)', falling back to the bare group number."""
    try:
        r = await d.client.get_group_info(int(group_id))
        if r.get("status") == "ok":
            name = (r.get("data") or {}).get("group_name") or ""
            if name:
                return "{}({})".format(name, group_id)
    except Exception as error:
        log.debug("group label lookup failed for %s: %s", group_id, error)
    return "群 " + str(group_id)
