"""Weather, translation, search, calculation, and information queries."""

import asyncio
import json
import logging
import os
import random
import re
import time

import aiohttp

from ..permission import (
    get_user_level, get_bot_role, get_group_config,
    add_master, remove_master, list_masters,
    save_group_config, can_moderate_target, LEVEL_MASTER, LEVEL_ADMIN,
)
from ..storage.runtime_paths import create_runtime_temp_file
from ..utils import atomic_write_json
from .common import CONFIG_PATH, _load, _save

log = logging.getLogger("qqbot")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _write_binary_fd(fd, payload):
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)


def _remove_temp_file(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    except OSError as error:
        log.debug("Temporary query file cleanup failed: %s", error)

async def cmd_hotboard(d, group_id, user_id, args, role, sender_card, message):
    """/热榜 [平台] — real hot board via uapis.cn."""
    from ..scheduler import BOARD_NAMES, build_detailed_hotboard, build_hotboard_forward_nodes, format_hotboard
    alias = {v: k for k, v in BOARD_NAMES.items()}
    board = (args.strip().lower() or "weibo")
    board = alias.get(board, board)
    from .. import uapi as _uapi
    if not _uapi.credits_available(d.config, "user", path="/misc/hotboard"):
        await d._reply(group_id, user_id, "今日积分额度用完了，明天再来")
        return
    data = await _uapi.uapi_get(d, "/misc/hotboard", params={"type": board}, kind="user")
    items = (data or {}).get("list") if isinstance(data, dict) else None
    if not items:
        await d._reply(group_id, user_id,
                       "没查到，支持的平台：" + "、".join(BOARD_NAMES.values()))
        return
    digest = await build_detailed_hotboard(d, board, items)
    if group_id:
        nodes = build_hotboard_forward_nodes(
            board, digest["items"], d.config.get("bot_qq", 0),
            limit=len(digest["items"]), summary=digest["summary"], details=digest["details"],
        )
        result = await d.client.send_group_forward_msg(int(group_id), nodes)
        status = (result or {}).get("status") if isinstance(result, dict) else result
        if status == "ok":
            return
    await d._reply(
        group_id, user_id,
        format_hotboard(board, digest["items"], limit=len(digest["items"]),
                        summary=digest["summary"], details=digest["details"]),
    )

async def _send_uapi_image(d, group_id, user_id, path, params, label):
    """Download a uapis.cn image endpoint to tmp and send it (bounded)."""
    from .. import uapi as _uapi
    if not _uapi.credits_available(d.config, "user", path=path):
        await d._reply(group_id, user_id, "今日积分额度用完了，明天再来")
        return
    result = await _uapi.uapi_get_binary(d, path, params=params, kind="user")
    if not result:
        await d._reply(group_id, user_id, label + "没取到，等会再试")
        return
    payload, ctype = result
    ext = ".png" if "png" in ctype else ".jpg"
    fd, tmp_path = create_runtime_temp_file("uapi_", ext)
    try:
        await asyncio.to_thread(_write_binary_fd, fd, payload)
        segments = [{"type": "image", "data": {"file": "file://" + tmp_path}}]
        if group_id:
            await d.client.send_group_msg(group_id, segments)
        else:
            await d.client.send_private_msg(user_id, segments)
    finally:
        await asyncio.to_thread(_remove_temp_file, tmp_path)

async def cmd_daily_news(d, group_id, user_id, args, role, sender_card, message):
    """/每日新闻 — daily news image via uapis.cn."""
    await _send_uapi_image(d, group_id, user_id, "/daily/news-image", None, "每日新闻")

async def cmd_bing_wallpaper(d, group_id, user_id, args, role, sender_card, message):
    """/必应壁纸 — Bing daily wallpaper via uapis.cn."""
    await _send_uapi_image(d, group_id, user_id, "/image/bing-daily", None, "必应壁纸")

async def cmd_epic_free(d, group_id, user_id, args, role, sender_card, message):
    """/epic免费 — Epic free games via uapis.cn."""
    from .. import uapi as _uapi
    if not _uapi.credits_available(d.config, "user", path="/game/epic-free"):
        await d._reply(group_id, user_id, "今日积分额度用完了，明天再来")
        return
    data = await _uapi.uapi_get(d, "/game/epic-free", kind="user")
    games = (data or {}).get("data") if isinstance(data, dict) else None
    if not games:
        await d._reply(group_id, user_id, "没查到 Epic 免费游戏，等会再试")
        return
    lines = ["【Epic 免费游戏】"]
    for g in games[:5]:
        title = str(g.get("title") or "?")
        price = str(g.get("original_price_desc") or "")
        state = "限免中" if g.get("is_free_now") else "即将限免"
        lines.append("· {}（{}，{}）".format(title, price, state))
    await d._reply(group_id, user_id, "\n".join(lines))

async def cmd_uapi_status(d, group_id, user_id, args, role, sender_card, message):
    """/积分 — show uapis.cn credit budget usage."""
    from .. import uapi as _uapi
    info = await _uapi.refresh_official_quota(d)
    lines = [
        "【uapis 积分额度】",
        "今日命令：剩 {}/{}".format(info["user_left"], info["user_cap"]),
        "今日预留（自动任务）：剩 {}/{}".format(info["auto_left"], info["auto_cap"]),
        "本地保护额度：本月已记 {}，上限 {}".format(
            info["month_used"], info["month_cap"]),
    ]
    official_left = info.get("official_month_remaining")
    official_cap = info.get("official_month_limit")
    if official_left is not None and official_cap:
        lines.append("官方额度：剩 {}/{}".format(official_left, official_cap))
    else:
        lines.append("官方额度：暂未从响应头获取")
    await d._reply(group_id, user_id, "\n".join(lines))

async def cmd_weather(d, group_id, user_id, args, role, sender_card, message):
    city = args.strip()
    if not city:
        await d._reply(group_id, user_id, "这样用：/天气 城市名，比如 /天气 杭州")
        return
    from .. import uapi as _uapi
    data = None
    if _uapi.credits_available(d.config, "user", path="/misc/weather"):
        data = await _uapi.uapi_get(d, "/misc/weather",
                                    params={"city": city[:20]}, kind="user")
    if data:
        reply = "【{}】{}，{}℃，{}，湿度{}%（{}）".format(
            data.get("city") or city,
            data.get("weather", "?"),
            data.get("temperature", "?"),
            "{} {}".format(data.get("wind_direction", ""), data.get("wind_power", "")).strip() or "微风",
            data.get("humidity", "?"),
            data.get("report_time", ""),
        )
    else:
        reply = "天气服务暂时没取到可靠数据，晚点再查吧"
    await d._reply(group_id, user_id, reply)

async def cmd_translate(d, group_id, user_id, args, role, sender_card, message):
    text = args.strip()
    if not text:
        await d._reply(group_id, user_id, "这样用：/translate 要翻译的文本")
        return
    # Try NapCat native translation first (free, fast)
    try:
        result = await d.client.call("translate_en2zh", {"text": text})
        if result.get("status") == "ok":
            data = result.get("data", {})
            translated = data.get("result") or data.get("text") or data.get("translated") or ""
            if translated:
                await d._reply(group_id, user_id, translated[:500])
                return
    except Exception as error:
        log.debug("Native translation failed; using AI fallback: %s", error)
    # Fallback to DeepSeek
    from ..ai import deepseek_chat
    reply = await deepseek_chat(d, "请将以下文本翻译成中文，只给出翻译结果：" + text)
    await d._reply(group_id, user_id, reply)

async def cmd_calc(d, group_id, user_id, args, role, sender_card, message):
    expr = args.strip()
    if not expr:
        await d._reply(group_id, user_id, "这样用：/calc 1+2*3")
        return
    try:
        result = _safe_calc(expr)
        await d._reply(group_id, user_id, expr + " = " + str(result))
    except Exception:
        await d._reply(group_id, user_id, "算不出来，表达式可能不太对")

def _safe_calc(expr):
    import ast
    import operator
    if len(expr) > 80:
        raise ValueError("expression too long")
    ops = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }
    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            left = _eval(node.left)
            right = _eval(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 6:
                raise ValueError("power too large")
            value = ops[type(node.op)](left, right)
            if abs(value) > 1_000_000_000_000:
                raise ValueError("result too large")
            return value
        if isinstance(node, ast.UnaryOp) and type(node.op) in ops:
            return ops[type(node.op)](_eval(node.operand))
        raise ValueError("unsupported expression")
    tree = ast.parse(expr, mode="eval")
    result = _eval(tree)
    if isinstance(result, float):
        return round(result, 8)
    return result

async def cmd_info(d, group_id, user_id, args, role, sender_card, message):
    mentions = d._extract_mentions(message)
    clean_args = re.sub(r"\[CQ:[^]]+\]", "", args).strip()
    # 群内：@用户 或 无参数看自己
    if group_id and not clean_args and not mentions:
        mentions = [user_id]
    if mentions:
        target = mentions[0]
        if group_id:
            r = await d.client.get_group_member_info(group_id, target, no_cache=True)
            if r.get("status") != "ok":
                await d._reply(group_id, user_id, "获取信息失败：" + str(r.get("msg") or r.get("wording") or r)[:200])
                return
            data = r.get("data", {})
            lines = [
                f"QQ: {data.get('user_id', target)}",
                f"昵称: {data.get('nickname', '')}",
            ]
            card = data.get("card", "")
            if card:
                lines.append(f"群名片: {card}")
            title = data.get("title", "")
            if title:
                lines.append(f"专属头衔: {title}")
            role_cn = {"owner": "群主", "admin": "管理员", "member": "成员"}.get(data.get("role", ""), "成员")
            lines.append(f"身份: {role_cn}")
            level = data.get("level", "")
            if level:
                lines.append(f"等级: Lv.{level}")
            sex = data.get("sex", "")
            if sex and sex != "unknown":
                lines.append(f"性别: {sex}")
            age = data.get("age", 0)
            if age:
                lines.append(f"年龄: {age}")
            join_time = data.get("join_time", 0)
            if join_time:
                import datetime as _dt
                jt = _dt.datetime.fromtimestamp(join_time).strftime("%Y-%m-%d %H:%M")
                lines.append(f"入群时间: {jt}")
            await d._reply(group_id, user_id, "\n".join(lines))
            return
        else:
            # 私聊：用 get_stranger_info 查任意人
            r = await d.client.get_stranger_info(target, no_cache=True)
            if r.get("status") != "ok":
                await d._reply(group_id, user_id, "获取信息失败：" + str(r.get("msg") or r.get("wording") or r)[:200])
                return
            data = r.get("data", {})
            lines = [
                f"QQ: {data.get('user_id', target)}",
                f"昵称: {data.get('nickname', '')}",
            ]
            sex = data.get("sex", "")
            if sex and sex != "unknown":
                lines.append(f"性别: {sex}")
            age = data.get("age", 0)
            if age:
                lines.append(f"年龄: {age}")
            await d._reply(group_id, user_id, "\n".join(lines))
            return
    # 有 args 但不是 @：解析为 QQ 号
    if clean_args:
        qq_match = re.search(r"\d{5,12}", clean_args)
        if qq_match:
            target = int(qq_match.group())
            if group_id:
                r = await d.client.get_group_member_info(group_id, target, no_cache=True)
                if r.get("status") == "ok":
                    data = r.get("data", {})
                    lines = [f"QQ: {data.get('user_id', target)}", f"昵称: {data.get('nickname', '')}"]
                    card = data.get("card", "")
                    if card:
                        lines.append(f"群名片: {card}")
                    role_cn = {"owner": "群主", "admin": "管理员", "member": "成员"}.get(data.get("role", ""), "成员")
                    lines.append(f"身份: {role_cn}")
                    await d._reply(group_id, user_id, "\n".join(lines))
                    return
            # 私聊或群内非成员：用 stranger_info
            r = await d.client.get_stranger_info(target, no_cache=True)
            if r.get("status") == "ok":
                data = r.get("data", {})
                lines = [f"QQ: {data.get('user_id', target)}", f"昵称: {data.get('nickname', '')}"]
                sex = data.get("sex", "")
                if sex and sex != "unknown":
                    lines.append(f"性别: {sex}")
                await d._reply(group_id, user_id, "\n".join(lines))
                return
            await d._reply(group_id, user_id, "获取信息失败")
            return
    await d._reply(group_id, user_id, "用法：/info [@用户] 或 /info QQ号")
