"""Grouped NapCat capability commands with feature and permission gates."""

import asyncio
import ipaddress
import json
import socket
from urllib.parse import urlparse

from ..permission import (
    LEVEL_ADMIN,
    LEVEL_GOWNER,
    LEVEL_MASTER,
    LEVEL_SUPER,
    can_moderate_target,
    get_bot_role,
    get_group_config,
    get_user_level,
    save_group_config,
)
from ..services.confirmations import (
    cancel_confirmation,
    create_confirmation,
    execute_confirmation,
)
from .uapi_extra import _image_url, _safe_public_url

_CATEGORY_ALIASES = {
    "消息": "message", "群管": "management", "待办": "todo", "相册": "album",
    "文件": "file", "好友": "friend", "账号": "account", "互动": "interaction",
    "实验": "experimental",
}


def _reply_id(message):
    if not isinstance(message, list):
        return 0
    for segment in message:
        if isinstance(segment, dict) and segment.get("type") == "reply":
            value = (segment.get("data") or {}).get("id")
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
    return 0


async def _level(dispatcher, group_id, user_id, role):
    return (await get_user_level(dispatcher, group_id, user_id, role))[0]


async def _require_write_permission(dispatcher, group_id, user_id, level):
    if group_id:
        if level < LEVEL_ADMIN:
            await dispatcher._reply(group_id, user_id, "这个写操作需要群管理身份")
            return False
        bot_role, _ = await get_bot_role(dispatcher, group_id)
        if bot_role not in ("admin", "owner"):
            await dispatcher._reply(group_id, user_id, "我现在不是管理员，做不了这个")
            return False
        return True
    if level < LEVEL_SUPER:
        await dispatcher._reply(None, user_id, "这个写操作只有最高主人能用")
        return False
    return True


async def _validated_public_url(value):
    url = _safe_public_url(value)
    if not url:
        return ""
    parsed = urlparse(url)
    if len(url) > 2048 or parsed.username or parsed.password:
        return ""
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo, parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except (OSError, UnicodeError):
        return ""
    if not addresses:
        return ""
    for address in addresses:
        try:
            if not ipaddress.ip_address(address[4][0]).is_global:
                return ""
        except (IndexError, TypeError, ValueError):
            return ""
    return url


async def _album_image_url(dispatcher, value, message):
    candidates = []
    direct = _image_url(value, message)
    if direct:
        candidates.append(direct)
    reply_id = _reply_id(message)
    if reply_id:
        try:
            result = await dispatcher.client.get_msg(reply_id)
        except (OSError, RuntimeError, TypeError, ValueError):
            result = None
        if isinstance(result, dict) and result.get("status") == "ok":
            replied = (result.get("data") or {}).get("message", [])
            reply_url = _image_url("", replied)
            if reply_url:
                candidates.append(reply_url)
    for candidate in candidates:
        validated = await _validated_public_url(candidate)
        if validated:
            return validated
    return ""


def _feature_enabled(dispatcher, group_id, category, level):
    if level >= LEVEL_SUPER:
        return True
    if not group_id:
        return bool(dispatcher.config.get("napcat_features", {}).get(category, False))
    group = dispatcher.config.get("groups", {}).get(str(group_id), {})
    local = group.get("napcat_features", {}) if isinstance(group, dict) else {}
    if category in local:
        return bool(local[category])
    return bool(dispatcher.config.get("napcat_features", {}).get(category, False))


async def _require_feature(dispatcher, group_id, user_id, role, category):
    level = await _level(dispatcher, group_id, user_id, role)
    if _feature_enabled(dispatcher, group_id, category, level):
        return level
    await dispatcher._reply(group_id, user_id, "这个分类还没开，让群主人用 /功能 {} on".format(
        next((name for name, key in _CATEGORY_ALIASES.items() if key == category), category)))
    return None


def _format_result(result):
    if not isinstance(result, dict):
        return str(result)
    if result.get("status") != "ok":
        return "调用失败：" + str(result.get("message") or result.get("msg") or result.get("wording") or result)[:180]
    data = result.get("data")
    return json.dumps(data, ensure_ascii=False, indent=2) if data is not None else "弄好了"


async def cmd_feature_center(d, group_id, user_id, args, role, sender_card, message):
    level = await _level(d, group_id, user_id, role)
    if level < LEVEL_MASTER:
        await d._reply(group_id, user_id, "这个只有群主人能改")
        return
    parts = args.strip().split()
    if len(parts) != 2 or parts[0] not in _CATEGORY_ALIASES or parts[1].lower() not in ("on", "off"):
        await d._reply(group_id, user_id, "用法：/功能 消息|群管|待办|相册|文件|好友|账号|互动|实验 on/off")
        return
    category = _CATEGORY_ALIASES[parts[0]]
    enabled = parts[1].lower() == "on"
    if group_id:
        group = d.config.setdefault("groups", {}).setdefault(str(group_id), {})
        group.setdefault("napcat_features", {})[category] = enabled
    else:
        d.config.setdefault("napcat_features", {})[category] = enabled
    save_group_config(d)
    await d._reply(group_id, user_id, "{}功能{}了".format(parts[0], "开" if enabled else "关"))


async def cmd_message_center(d, group_id, user_id, args, role, sender_card, message):
    level = await _require_feature(d, group_id, user_id, role, "message")
    if level is None:
        return
    parts = args.strip().split()
    sub = parts[0] if parts else "帮助"
    if sub in ("帮助", "help"):
        await d._reply(group_id, user_id,
            "/消息 表情列表 <消息ID> <表情ID>\n/消息 语音文字 <消息ID>\n/消息 闪传 <文件集ID>",
            force_forward=True, kind="help", title="消息功能", role_hint=role)
        return
    if sub == "表情列表" and len(parts) >= 3:
        result = await d.client.get_emoji_likes(parts[1], parts[2], group_id=group_id)
    elif sub == "语音文字" and len(parts) >= 2:
        result = await d.client.fetch_ptt_text(parts[1])
    elif sub == "闪传" and len(parts) >= 2:
        if not await _require_write_permission(d, group_id, user_id, level):
            return
        result = await d.client.send_flash_msg(parts[1], group_id=group_id,
                                               user_id=None if group_id else user_id)
    else:
        await d._reply(group_id, user_id, "参数不对，发 /消息 帮助 看用法")
        return
    await d._reply(group_id, user_id, _format_result(result), title="消息功能结果", role_hint=role)


async def cmd_todo_center(d, group_id, user_id, args, role, sender_card, message):
    level = await _require_feature(d, group_id, user_id, role, "todo")
    if level is None:
        return
    if not group_id:
        await d._reply(None, user_id, "群待办只能在群里用")
        return
    parts = args.strip().split()
    sub = parts[0] if parts else "帮助"
    message_id = parts[1] if len(parts) > 1 else _reply_id(message)
    if sub in ("添加", "创建", "完成", "取消"):
        if not await _require_write_permission(d, group_id, user_id, level):
            return
    if sub in ("添加", "创建") and message_id:
        result = await d.client.set_group_todo(group_id, message_id=message_id)
    elif sub == "完成" and message_id:
        result = await d.client.complete_group_todo(group_id, message_id=message_id)
    elif sub == "取消" and message_id:
        result = await d.client.cancel_group_todo(group_id, message_id=message_id)
    else:
        await d._reply(group_id, user_id, "回复一条消息使用 /待办 添加|完成|取消，也可以在后面写消息ID")
        return
    await d._reply(group_id, user_id, _format_result(result), role_hint=role)


async def cmd_album_center(d, group_id, user_id, args, role, sender_card, message):
    level = await _require_feature(d, group_id, user_id, role, "album")
    if level is None:
        return
    if not group_id:
        await d._reply(None, user_id, "群相册只能在群里用")
        return
    command = args.strip().split(maxsplit=1)
    sub = command[0] if command else "列表"
    rest = command[1] if len(command) > 1 else ""
    if sub == "列表":
        result = await d.client.get_group_album_list(group_id)
    elif sub == "内容" and rest:
        result = await d.client.get_group_album_media_list(group_id, rest.split()[0])
    elif sub == "上传":
        parts = rest.split(maxsplit=2)
        if len(parts) < 2:
            result = None
        elif not await _require_write_permission(d, group_id, user_id, level):
            return
        else:
            image_url = await _album_image_url(
                d, parts[2] if len(parts) > 2 else "", message)
            if not image_url:
                await d._reply(group_id, user_id, "只接受公开 http/https 图片URL，或回复一张图片上传；不支持服务器本地路径")
                return
            result = await d.client.upload_group_album_image(
                group_id, parts[0], parts[1], image_url)
    elif sub == "评论":
        parts = rest.split(maxsplit=2)
        if len(parts) < 3:
            result = None
        elif not await _require_write_permission(d, group_id, user_id, level):
            return
        else:
            result = await d.client.comment_group_album_media(
                group_id, parts[0], parts[1], parts[2][:500])
    elif sub == "点赞":
        parts = rest.split(maxsplit=2)
        if len(parts) < 2:
            result = None
        elif not await _require_write_permission(d, group_id, user_id, level):
            return
        else:
            result = await d.client.set_group_album_media_like(
                group_id, parts[0], parts[1], parts[2] if len(parts) > 2 else "")
    elif sub == "删除":
        parts = rest.split(maxsplit=1)
        if len(parts) < 2:
            result = None
        elif not await _require_write_permission(d, group_id, user_id, level):
            return
        else:
            params = {"group_id": str(group_id), "album_id": str(parts[0]), "lloc": str(parts[1])}
            if level >= LEVEL_SUPER:
                result = await d.client.call("del_group_album_media", params)
            else:
                code = create_confirmation(group_id, user_id, "del_group_album_media", params,
                                           "删除群相册媒体 {}".format(parts[1]))
                await d._reply(group_id, user_id, "要删的话发 /确认 {}，一分钟内有效".format(code))
                return
    else:
        result = None
    if result is None:
        await d._reply(group_id, user_id,
            "/相册 列表\n/相册 内容 <album_id>\n/相册 上传 <album_id> <相册名> [公开图片URL]（也可回复图片）\n"
            "/相册 评论 <album_id> <lloc> <评论内容>\n/相册 点赞 <album_id> <batch_id> [lloc]\n"
            "/相册 删除 <album_id> <lloc>",
            force_forward=True, kind="help", title="群相册功能", role_hint=role)
        return
    await d._reply(group_id, user_id, _format_result(result), title="群相册结果", role_hint=role)


async def cmd_group_management_center(d, group_id, user_id, args, role, sender_card, message):
    level = await _require_feature(d, group_id, user_id, role, "management")
    if level is None:
        return
    if not group_id or level < LEVEL_ADMIN:
        await d._reply(group_id, user_id, "这个需要群管理身份")
        return
    parts = args.strip().split(maxsplit=3)
    sub = parts[0] if parts else "帮助"
    if sub == "详情":
        result = await d.client.get_group_detail_info(group_id)
    elif sub == "打卡列表":
        result = await d.client.get_group_signed_list(group_id)
    elif sub == "群备注" and len(parts) >= 2:
        result = await d.client.set_group_remark(group_id, " ".join(parts[1:]))
    elif sub == "批量踢" and len(parts) >= 2:
        targets = [int(value) for value in parts[1].replace("，", ",").split(",") if value.isdigit()]
        for target in targets:
            allowed, error = await can_moderate_target(d, group_id, user_id, target, role)
            if not allowed:
                await d._reply(group_id, user_id, "不能处理 {}：{}".format(target, error))
                return
        params = {"group_id": str(group_id), "user_id": [str(value) for value in targets[:20]], "reject_add_request": False}
        if level >= LEVEL_SUPER:
            result = await d.client.call("set_group_kick_members", params)
        else:
            code = create_confirmation(group_id, user_id, "set_group_kick_members", params,
                                       "批量踢出 {} 人".format(len(targets[:20])))
            await d._reply(group_id, user_id, "确认没选错人就发 /确认 {}".format(code))
            return
    elif sub == "加群方式" and len(parts) >= 2 and level >= LEVEL_GOWNER:
        result = await d.client.set_group_add_option(
            group_id, int(parts[1]), parts[2] if len(parts) > 2 else "",
            parts[3] if len(parts) > 3 else "")
    else:
        await d._reply(group_id, user_id,
            "/群管 详情\n/群管 打卡列表\n/群管 群备注 <内容>\n/群管 批量踢 <QQ,QQ>\n/群管 加群方式 <类型> [问题] [答案]",
            force_forward=True, kind="help", title="群管扩展功能", role_hint=role)
        return
    await d._reply(group_id, user_id, _format_result(result), title="群管结果", role_hint=role)


async def cmd_file_center(d, group_id, user_id, args, role, sender_card, message):
    level = await _require_feature(d, group_id, user_id, role, "file")
    if level is None:
        return
    parts = args.strip().split(maxsplit=3)
    sub = parts[0] if parts else "列表"
    if sub == "列表" and group_id:
        result = await d.client.get_group_root_files(group_id)
    elif sub == "状态" and group_id:
        result = await d.client.get_group_file_system_info(group_id)
    elif sub == "在线列表" and len(parts) >= 2 and level >= LEVEL_SUPER:
        result = await d.client.get_online_file_msg(parts[1])
    elif sub == "在线发送" and len(parts) >= 3 and level >= LEVEL_SUPER:
        result = await d.client.send_online_file(parts[1], parts[2], parts[3] if len(parts) > 3 else "")
    elif sub == "在线接收" and len(parts) >= 4 and level >= LEVEL_SUPER:
        result = await d.client.receive_online_file(parts[1], parts[2], parts[3], approve=True)
    elif sub == "在线拒绝" and len(parts) >= 4 and level >= LEVEL_SUPER:
        result = await d.client.receive_online_file(parts[1], parts[2], parts[3], approve=False)
    else:
        await d._reply(group_id, user_id,
            "/文件 列表\n/文件 状态\n最高主人：/文件 在线列表 <QQ>\n/文件 在线发送 <QQ> <路径> [文件名]\n/文件 在线接收|在线拒绝 <QQ> <消息ID> <元素ID>",
            force_forward=True, kind="help", title="文件功能", role_hint=role)
        return
    await d._reply(group_id, user_id, _format_result(result), title="文件功能结果", role_hint=role)


async def cmd_experimental_center(d, group_id, user_id, args, role, sender_card, message):
    level = await _require_feature(d, group_id, user_id, role, "experimental")
    if level is None or level < LEVEL_SUPER:
        await d._reply(group_id, user_id, "实验功能只给最高主人看")
        return
    await d._reply(
        group_id, user_id,
        "实验能力目前包含闪传、文件集、Guild兼容和自定义表情详情。"
        "只有接口能力探测为 supported 后才会开放写操作。\n"
        "可先用 /api napcat 查看支持状态。",
        force_forward=True, kind="help", title="实验功能", role_hint=role,
    )

async def cmd_friend_center(d, group_id, user_id, args, role, sender_card, message):
    level = await _require_feature(d, group_id, user_id, role, "friend")
    if level is None or level < LEVEL_SUPER:
        await d._reply(group_id, user_id, "好友是全局账号数据，只有最高主人能管")
        return
    parts = args.strip().split(maxsplit=2)
    sub = parts[0] if parts else "帮助"
    if sub == "单向列表":
        result = await d.client.get_unidirectional_friend_list()
    elif sub == "备注" and len(parts) >= 3:
        result = await d.client.set_friend_remark(parts[1], parts[2])
    elif sub == "删除" and len(parts) >= 2:
        if not parts[1].isdigit():
            await d._reply(group_id, user_id, "好友 QQ 号只能写数字")
            return
        code = create_confirmation(
            group_id, user_id, "delete_friend",
            {"user_id": parts[1], "temp_block": False, "temp_both_del": True},
            "双向删除好友 {}".format(parts[1]),
        )
        await d._reply(group_id, user_id, "确认删除的话请在一分钟内发送 /确认 {}".format(code))
        return
    elif sub == "可疑申请":
        result = await d.client.get_doubt_friends_add_request()
    else:
        await d._reply(group_id, user_id,
            "/好友 单向列表\n/好友 备注 <QQ> <备注>\n/好友 删除 <QQ>\n/好友 可疑申请",
            force_forward=True, kind="help", title="好友管理", role_hint=role)
        return
    await d._reply(group_id, user_id, _format_result(result), title="好友管理结果", role_hint=role)


async def cmd_account_center(d, group_id, user_id, args, role, sender_card, message):
    level = await _require_feature(d, group_id, user_id, role, "account")
    if level is None or level < LEVEL_SUPER:
        await d._reply(group_id, user_id, "账号资料只有最高主人能改")
        return
    parts = args.strip().split(maxsplit=3)
    sub = parts[0] if parts else "帮助"
    if sub == "资料" and len(parts) >= 2:
        result = await d.client.set_qq_profile(parts[1], parts[2] if len(parts) > 2 else "",
                                               int(parts[3]) if len(parts) > 3 else 0)
    elif sub == "状态" and len(parts) >= 4:
        result = await d.client.set_diy_online_status(parts[1], parts[2], parts[3])
    elif sub == "表情列表":
        result = await d.client.fetch_custom_face_detail()
    elif sub == "加表情" and len(parts) >= 2:
        result = await d.client.add_custom_face(parts[1])
    else:
        await d._reply(group_id, user_id,
            "/账号 资料 <昵称> [签名] [性别0/1/2]\n/账号 状态 <face_id> <face_type> <文字>\n/账号 表情列表\n/账号 加表情 <文件路径>",
            force_forward=True, kind="help", title="账号功能", role_hint=role)
        return
    await d._reply(group_id, user_id, _format_result(result), title="账号功能结果", role_hint=role)


async def cmd_interaction_center(d, group_id, user_id, args, role, sender_card, message):
    level = await _require_feature(d, group_id, user_id, role, "interaction")
    if level is None:
        return
    parts = args.strip().split()
    if parts and parts[0] == "群分享" and group_id:
        result = await d.client.ark_share_group(group_id)
    elif parts and parts[0] == "闪传" and len(parts) >= 2:
        if not await _require_write_permission(d, group_id, user_id, level):
            return
        result = await d.client.send_flash_msg(parts[1], group_id=group_id,
                                               user_id=None if group_id else user_id)
    else:
        await d._reply(group_id, user_id, "/互动 群分享\n/互动 闪传 <文件集ID>",
                       force_forward=True, kind="help", title="互动功能", role_hint=role)
        return
    await d._reply(group_id, user_id, _format_result(result), role_hint=role)


async def cmd_automation_center(d, group_id, user_id, args, role, sender_card, message):
    level = await _level(d, group_id, user_id, role)
    if level < LEVEL_GOWNER:
        await d._reply(group_id, user_id, "自动化要群主人或群主来开")
        return
    sub = args.strip().lower()
    if sub in ("on", "off"):
        target = d.config.setdefault("groups", {}).setdefault(str(group_id), {}).setdefault("automation", {}) if group_id else d.config.setdefault("automation", {})
        target["enabled"] = sub == "on"
        save_group_config(d)
        await d._reply(group_id, user_id, "自动化{}了".format("开" if sub == "on" else "关"))
        return
    config = get_group_config(d, group_id).get("automation", d.config.get("automation", {}))
    await d._reply(group_id, user_id, "自动化状态：{}\n用法：/自动化 on/off".format(
        "开启" if config.get("enabled") else "关闭"))


async def cmd_confirm_action(d, group_id, user_id, args, role, sender_card, message):
    ok, text = await execute_confirmation(d, args.strip(), user_id, group_id, role)
    await d._reply(group_id, user_id, text)


async def cmd_cancel_confirmation(d, group_id, user_id, args, role, sender_card, message):
    ok = cancel_confirmation(args.strip(), user_id)
    await d._reply(group_id, user_id, "取消了" if ok else "没找到这个确认码")
