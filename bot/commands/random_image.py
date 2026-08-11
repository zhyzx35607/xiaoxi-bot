"""Random image command backed by the Mukyu image service."""

import logging
import re
import time

from ..integrations.mukyu import MukyuError, fetch_random_image
from ..permission import LEVEL_MASTER, get_user_level


log = logging.getLogger("qqbot")

RANDOM_IMAGE_HELP = """格式：
/随机图 [内容标签] [标签关系] [画面方向] [清晰度] [收藏门槛] [AI范围] [作品类型] [分级]
/pixiv图 与 /随机图 完全相同。

内容标签：
- 可以直接写任意 Pixiv 标签：初音ミク、猫、原神、风景等。
- 多标签推荐写：标签=标签1,标签2；也支持中文逗号。
- 标签最多 20 个，每个最多 64 个字符；不写标签就是全范围随机。

标签关系：
- 或 / or / 任一：命中任意一个标签，默认值。
- 且 / and / 全部：必须同时命中全部标签。

画面方向：
- 横图 / 横屏 / landscape
- 竖图 / 竖屏 / portrait
- 方图 / 方形 / square
- 不写则不限方向。

清晰度：
- 高清 / hd：至少 100 万像素。
- 超清 / uhd：至少 400 万像素。
- 不写则不限制像素数。

收藏门槛：
- 收藏=数字 或 热门=数字，例如 收藏=1000。
- 表示只选收藏数不低于该数字的作品。

AI 范围：
- 非AI：只选非 AI 作品。
- AI：只选 AI 作品。
- 不写则两者都可以。

作品类型：
- 插画 / 漫画 / 动图
- 不写则类型不限。

分级：
- 全年龄 / 安全：只选全年龄作品，默认值。
- R18 / 成人：只选 R18 作品。
- 混合：全年龄与 R18 都可以。
- R18 和混合仅最高主人、当前群配置的群主人可用；其他人始终按全年龄请求。

示例：
/随机图
/随机图 初音ミク 竖图 高清
/随机图 标签=初音ミク,ボーカロイド 且 非AI 插画 收藏=1000
/随机图 原神 或 方图 超清
/随机图 标签=风景 横图 全年龄
/随机图 标签=碧蓝档案 R18 竖图（仅最高主人、群主人）"""

_ORIENTATIONS = {
    "横图": "landscape", "横屏": "landscape", "landscape": "landscape",
    "竖图": "portrait", "竖屏": "portrait", "portrait": "portrait",
    "方图": "square", "方形": "square", "square": "square",
}
_TYPE_OPTIONS = {"插画": 0, "漫画": 1, "动图": 2}
_AI_OPTIONS = {"非ai": 0, "非AI": 0, "ai": 1, "AI": 1}
_R18_OPTIONS = {"全年龄": 0, "安全": 0, "r18": 1, "R18": 1, "成人": 1, "混合": 2}


def _parse_random_image_args(args):
    options = {
        "r18": 0, "tags": [], "tag_mode": "or", "orientation": None,
        "min_pixels": None, "min_bookmarks": None,
        "ai_type": None, "illust_type": None,
    }
    raw = str(args or "").strip()
    if not raw:
        return options
    tokens = [item for item in re.split(r"\s+", raw) if item]
    for token in tokens:
        if token in _R18_OPTIONS:
            options["r18"] = _R18_OPTIONS[token]
        elif token in _ORIENTATIONS:
            options["orientation"] = _ORIENTATIONS[token]
        elif token in _TYPE_OPTIONS:
            options["illust_type"] = _TYPE_OPTIONS[token]
        elif token in _AI_OPTIONS:
            options["ai_type"] = _AI_OPTIONS[token]
        elif token.lower() in {"and", "且", "全部"}:
            options["tag_mode"] = "and"
        elif token.lower() in {"or", "或", "任一"}:
            options["tag_mode"] = "or"
        elif token in {"高清", "hd"}:
            options["min_pixels"] = 1_000_000
        elif token in {"超清", "uhd"}:
            options["min_pixels"] = 4_000_000
        elif token.startswith(("收藏=", "热门=")):
            value = token.split("=", 1)[1]
            if value.isdigit():
                options["min_bookmarks"] = min(10_000_000, int(value))
        elif token.startswith(("标签=", "tag=", "tags=")):
            value = token.split("=", 1)[1]
            options["tags"].extend(re.split(r"[,，]", value))
        else:
            options["tags"].extend(re.split(r"[,，]", token))
    options["tags"] = [tag.strip()[:64] for tag in options["tags"] if tag.strip()][:20]
    return options


def _cooldown_allowed(dispatcher, group_id, user_id):
    config = dispatcher.config.get("mukyu_images", {})
    cooldown = max(1, min(120, int(config.get("command_cooldown_seconds", 10) or 10)))
    now = time.monotonic()
    records = getattr(dispatcher, "_mukyu_command_cooldowns", None)
    if records is None:
        records = {}
        dispatcher._mukyu_command_cooldowns = records
    key = (int(group_id or 0), int(user_id or 0))
    if now - float(records.get(key, 0) or 0) < cooldown:
        return False
    records[key] = now
    if len(records) > 2000:
        dispatcher._mukyu_command_cooldowns = {
            item_key: timestamp for item_key, timestamp in records.items()
            if now - timestamp < cooldown * 2
        }
    return True


async def cmd_random_image(d, group_id, user_id, args, role, sender_card, message):
    if str(args or "").strip().lower() in {"help", "帮助", "用法"}:
        await d._reply(group_id, user_id, RANDOM_IMAGE_HELP)
        return
    if not _cooldown_allowed(d, group_id, user_id):
        await d._reply(group_id, user_id, "选图太快啦，稍等几秒再试")
        return

    options = _parse_random_image_args(args)
    level, _ = await get_user_level(d, group_id, user_id, role)
    if options["r18"] and level < LEVEL_MASTER:
        await d._reply(group_id, user_id, "R18 标签范围只对最高主人和本群群主人开放")
        return
    try:
        image = await fetch_random_image(d, **options)
    except MukyuError as error:
        log.info("Random image command unavailable: %s", error)
        await d._reply(group_id, user_id, "这次没选到合适的图，稍后再试一下吧")
        return

    segments = [{"type": "image", "data": {"file": image.url}}]
    if group_id:
        result = await d.client.send_group_msg(group_id, segments)
    else:
        result = await d.client.send_private_msg(user_id, segments)
    if isinstance(result, dict) and (
            result.get("status") == "timeout" or result.get("error_kind") == "timeout"):
        log.warning(
            "Random image send result unconfirmed after timeout: group=%s user=%s image_id=%s",
            group_id, user_id, image.image_id,
        )
        return
    if not isinstance(result, dict) or result.get("status") != "ok":
        await d._reply(group_id, user_id, "图片取到了，但发送失败了，稍后再试")
