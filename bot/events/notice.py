# bot/notice_handler.py - Group notices, poke, badwords, admin changes
import asyncio
import json, logging, time, re, os
import threading
from ..permission import get_group_config, is_group_enabled, save_group_config
from ..utils import atomic_write_json

log = logging.getLogger("qqbot")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_GROUP_FILES_PATH = os.path.join(_ROOT, "data", "group_files.json")
# Serializes the read-modify-write in _record_group_upload across worker threads.
_GROUP_FILES_LOCK = threading.Lock()


def _read_json_file(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        log.debug("Optional JSON state read failed: %s", error)
        return default


def _record_group_upload(group_id, entry):
    """Append a group-file upload record as one locked read-modify-write."""
    with _GROUP_FILES_LOCK:
        data = _read_json_file(_GROUP_FILES_PATH, {})
        if not isinstance(data, dict):
            data = {}
        files = data.setdefault(str(group_id), [])
        files.append(entry)
        data[str(group_id)] = files[-200:]
        atomic_write_json(_GROUP_FILES_PATH, data, indent=2)


# 只把与群秩序相关的通知喂给 Agent 事件流，戳一戳/表情等高频噪声不喂
_OBSERVED_NOTICE_TYPES = {
    "group_increase", "group_decrease", "group_admin",
    "group_recall", "group_ban", "group_upload",
}


def _describe_notice(event):
    notice_type = event.get("notice_type", "")
    sub_type = event.get("sub_type", "")
    user_id = event.get("user_id", 0)
    if notice_type == "group_increase":
        return "用户{} 加入了本群".format(user_id)
    if notice_type == "group_decrease":
        if sub_type == "kick_me":
            return "小汐被移出了本群"
        action = "被移出本群" if sub_type == "kick" else "退出了本群"
        return "用户{} {}".format(user_id, action)
    if notice_type == "group_admin":
        return "用户{} 的管理员身份变更（{}）".format(user_id, sub_type)
    if notice_type == "group_recall":
        return "用户{} 撤回了一条消息（message_id={}）".format(
            event.get("operator_id", 0), event.get("message_id", 0))
    if notice_type == "group_ban":
        if sub_type == "lift_ban":
            return "用户{} 被解除禁言".format(user_id)
        return "用户{} 被禁言 {} 秒".format(user_id, event.get("duration", 0))
    if notice_type == "group_upload":
        file_info = event.get("file", {}) or {}
        name = file_info.get("name") or file_info.get("file_name") or ""
        return "用户{} 上传了群文件 {}".format(user_id, str(name)[:80])
    return ""


async def _observe_notice(dispatcher, event):
    """Trim a group notice into the Agent event stream (observe only)."""
    agent_settings = dispatcher.config.get("agent", {})
    if not agent_settings.get("observation_enabled", False):
        return
    runtime = getattr(dispatcher, "agent_runtime", None)
    if runtime is None:
        return
    group_id = event.get("group_id", 0)
    notice_type = event.get("notice_type", "")
    if not group_id or notice_type not in _OBSERVED_NOTICE_TYPES:
        return
    description = _describe_notice(event)
    if not description:
        return
    user_id = event.get("user_id") or event.get("operator_id") or event.get("target_id") or 0
    try:
        await runtime.observe(dispatcher, {
            "post_type": "notice",
            "user_id": user_id,
            "group_id": group_id,
            "message_type": "group",
            "raw_message": "[notice] " + description,
            "time": event.get("time", time.time()),
            "sender": {"role": "member"},
        })
    except Exception:
        log.exception("Agent notice observation failed")


async def handle_notice(dispatcher, event):
    notice_type = event.get("notice_type", "")
    group_id = event.get("group_id", 0)
    if group_id and not is_group_enabled(dispatcher, group_id):
        return
    if notice_type == "group_increase":
        await handle_group_increase(dispatcher, event)
    elif notice_type == "group_decrease":
        await handle_group_decrease(dispatcher, event)
    elif notice_type == "group_admin":
        await handle_group_admin(dispatcher, event)
    elif notice_type == "group_recall":
        await handle_group_recall(dispatcher, event)
    elif notice_type == "group_upload":
        await handle_group_upload(dispatcher, event)
    elif notice_type == "group_ban":
        await handle_group_ban(dispatcher, event)
    elif notice_type == "essence":
        await handle_essence(dispatcher, event)
    elif notice_type == "group_card":
        await handle_group_card(dispatcher, event)
    elif notice_type == "group_msg_emoji_like":
        await handle_group_msg_emoji_like(dispatcher, event)
    elif notice_type == "friend_add":
        await handle_friend_add(dispatcher, event)
    elif notice_type == "bot_offline":
        await handle_bot_offline(dispatcher, event)
    elif notice_type == "notify":
        sub = event.get("sub_type", "")
        if sub == "poke":
            await handle_poke(dispatcher, event)
        elif sub == "title":
            await handle_title_change(dispatcher, event)
        elif sub == "group_name":
            await handle_group_name_change(dispatcher, event)
        elif sub == "profile_like":
            await handle_profile_like(dispatcher, event)
        elif sub == "input_status":
            log.debug("Input status notice user=%s status=%s",
                      event.get("user_id"), event.get("status_text", ""))
        elif sub == "gray_tip":
            from ..security import handle_gray_tip
            await handle_gray_tip(dispatcher, event)
        else:
            log.info("Notify event subtype=%s group=%s user=%s target=%s keys=%s",
                     sub, event.get("group_id"), event.get("user_id"),
                     event.get("target_id"), sorted(event.keys()))
    else:
        log.info("Unhandled notice type=%s keys=%s", notice_type, sorted(event.keys()))
    await _observe_notice(dispatcher, event)


async def _generate_welcome_text(dispatcher, nickname, sex=""):
    """Generate a short, friendly welcome message using AI (DeepSeek)."""
    sex_part = "（" + sex + "）" if sex else ""
    prompt = f"新生「{nickname}」{sex_part}加入了群聊，请用一句简短（15字以内）有趣友好的话欢迎ta。自然口语化，不用emoji。直接回复内容，不用任何前缀。"
    try:
        from ..ai import _call_deepseek
        from ..ai.reply import strip_command_prefix
        msg = [{"role": "user", "content": prompt}]
        reply = await _call_deepseek(dispatcher.config, msg, max_tokens=30, temperature=0.8,
                                      session=dispatcher.client.session)
        if reply and len(reply.strip()) > 3:
            # 昵称由新成员控制，AI 可能复述出 "/命令" 样式文本；欢迎语会经
            # message_sent 回环被当最高主人命令执行，外发前中和行首命令前缀。
            return strip_command_prefix(reply.strip()[:30])
    except Exception as error:
        log.debug("Welcome text generation failed: %s", error)
    return f"欢迎 {nickname} 哦～"


async def handle_group_increase(dispatcher, event):
    group_id = event.get("group_id", 0)
    if not is_group_enabled(dispatcher, group_id):
        return
    user_id = event.get("user_id", 0)
    from event_policy import automation_enabled, allow_event
    if not automation_enabled(dispatcher.config, "welcome", True) or not allow_event("welcome", group_id, 3):
        return
    gcfg = get_group_config(dispatcher, group_id)
    wm = gcfg.get("welcome_msg", {})
    if not wm.get("enabled", True):
        return
    nickname = str(user_id)
    sex = ""
    try:
        info = await dispatcher.client.get_group_member_info(group_id, user_id)
        if info.get("status") == "ok":
            data = info.get("data", {})
            nickname = data.get("card") or data.get("nickname", str(user_id))
            sex = data.get("sex", "")
    except Exception as error:
        log.debug("Group member lookup failed: %s", error)
    if automation_enabled(dispatcher.config, "ai_welcome", True):
        msg = await _generate_welcome_text(dispatcher, nickname, sex)
    else:
        msg = (wm.get("template") or "欢迎 {nickname}").replace("{nickname}", nickname)
    await dispatcher.client.send_group_msg(group_id, msg)
async def handle_group_decrease(dispatcher, event):
    group_id = event.get("group_id", 0)
    if not is_group_enabled(dispatcher, group_id):
        return
    user_id = event.get("user_id", 0)
    sub_type = event.get("sub_type", "")

    if sub_type == "kick_me":
        log.info("Bot kicked from group %s", group_id)
        groups = dispatcher.config.setdefault("groups", {})
        gid = str(group_id)
        if gid in groups and isinstance(groups[gid], dict):
            groups[gid]["enabled"] = False
            await asyncio.to_thread(save_group_config, dispatcher)
        return

    uid_str = str(user_id)
    # Resolve nickname via get_stranger_info (works even after user left)
    nickname = uid_str
    try:
        info = await dispatcher.client.get_stranger_info(user_id, no_cache=True)
        if info.get("status") == "ok":
            data = info.get("data", {})
            nickname = data.get("nickname", "") or data.get("card", "") or uid_str
    except Exception as error:
        log.debug("Departed member lookup failed: %s", error)

    action = "被移出群聊" if sub_type == "kick" else "离开了群聊"
    text = f"{nickname}({user_id}) {action}"

    # Build message with QQ avatar image
    avatar_url = "https://q1.qlogo.cn/g?b=qq&nk=" + str(user_id) + "&s=640"
    msg_segments = [
        {"type": "image", "data": {"file": avatar_url}},
        {"type": "text", "data": {"text": "\n" + text}},
    ]
    await dispatcher.client.send_group_msg(group_id, msg_segments)
async def handle_group_admin(dispatcher, event):
    """Monitor bot admin changes and notify the owner on capability loss."""
    group_id = event.get("group_id", 0)
    user_id = event.get("user_id", 0)
    sub_type = event.get("sub_type", "")
    bot_qq = dispatcher.config["bot_qq"]

    if user_id == bot_qq:
        new_role = "admin" if sub_type == "set" else "member"
        log.info("Bot admin status changed: g=%s role=%s", group_id, new_role)
        if new_role != "member":
            return
        owner_id = int(dispatcher.config.get("bot_owner") or 0)
        if not owner_id:
            return
        now = time.time()
        notices = getattr(dispatcher, "_bot_admin_loss_notices", None)
        if not isinstance(notices, dict):
            notices = {}
            dispatcher._bot_admin_loss_notices = notices
        if now - float(notices.get(group_id, 0) or 0) < 6 * 3600:
            return
        notices[group_id] = now
        try:
            await dispatcher.client.send_private_msg(
                owner_id,
                "机器人在群 {} 的管理员身份已被取消，禁言、踢人、群公告等管理功能将暂时不可用。".format(
                    group_id),
            )
        except Exception as error:
            log.warning("Bot admin loss notification failed g=%s: %s", group_id, error)


async def handle_group_recall(dispatcher, event):
    """Group message recall notice - log for now."""
    group_id = event.get("group_id", 0)
    operator_id = event.get("operator_id", 0)
    message_id = event.get("message_id", 0)
    log.debug("Message recalled in g=%s by %s mid=%s", group_id, operator_id, message_id)


async def handle_group_upload(dispatcher, event):
    group_id = event.get("group_id", 0)
    from event_policy import automation_enabled, allow_event
    if not automation_enabled(dispatcher.config, "file_notice", True) or not allow_event("file_notice", group_id, 10):
        return
    user_id = event.get("user_id", 0)
    file_info = event.get("file", {}) or {}
    name = file_info.get("name") or file_info.get("file_name") or file_info.get("id") or "未知文件"
    file_id = file_info.get("id") or file_info.get("file_id") or ""
    busid = file_info.get("busid") or file_info.get("bus_id") or ""
    size = file_info.get("size") or file_info.get("file_size") or 0
    log.info("Group file uploaded: group=%s user=%s size=%s", group_id, user_id, size)
    try:
        await asyncio.to_thread(_record_group_upload, group_id, {
            "ts": time.time(),
            "user_id": user_id,
            "name": name,
            "file_id": file_id,
            "busid": busid,
            "size": size,
        })
    except Exception as e:
        log.error("Save group upload notice failed: %s", e)


async def handle_group_ban(dispatcher, event):
    group_id = event.get("group_id", 0)
    user_id = event.get("user_id", 0)
    operator_id = event.get("operator_id", 0)
    sub_type = event.get("sub_type", "")
    duration = event.get("duration", 0)
    log.info("Group ban notice: g=%s user=%s op=%s subtype=%s duration=%s",
             group_id, user_id, operator_id, sub_type, duration)


async def handle_essence(dispatcher, event):
    group_id = event.get("group_id", 0)
    sender_id = event.get("sender_id", 0)
    operator_id = event.get("operator_id", 0)
    message_id = event.get("message_id", 0)
    sub_type = event.get("sub_type", "")
    log.info("Essence notice: g=%s mid=%s sender=%s op=%s subtype=%s",
             group_id, message_id, sender_id, operator_id, sub_type)


async def handle_poke(dispatcher, event):
    """Handle poke (戳一戳) - poke back if bot is poked."""
    group_id = event.get("group_id", 0)
    from event_policy import automation_enabled, allow_event
    if not automation_enabled(dispatcher.config, "auto_poke", True):
        return
    user_id = event.get("user_id", 0)
    target_id = event.get("target_id", 0)
    bot_qq = dispatcher.config["bot_qq"]

    if target_id != bot_qq:
        return
    if not allow_event("poke", group_id or user_id, 5):
        return

    if group_id:
        # 群内戳一戳
        gcfg = get_group_config(dispatcher, group_id)
        feats = gcfg.get("features", {})
        if not feats.get("auto_poke", True):
            return
        log.info("Bot poked by %s in group %s, poking back", user_id, group_id)
        try:
            await dispatcher.client.call("group_poke", {
                "group_id": group_id,
                "user_id": user_id
            })
        except Exception as e:
            log.error("Poke back failed: %s", e)
    else:
        # 好友私聊戳一戳
        log.info("Bot poked by %s in private, poking back", user_id)
        try:
            await dispatcher.client.friend_poke(user_id)
        except Exception as e:
            log.error("Friend poke back failed: %s", e)


async def check_bad_words(dispatcher, group_id, user_id, raw_message, message_id):
    gcfg = get_group_config(dispatcher, group_id)
    bw = gcfg.get("bad_words", {})
    if not bw.get("enabled", True):
        return False
    lower = raw_message.lower()
    for word in bw.get("words", []):
        word = (word or "").strip()
        if not word:
            continue
        matched = False
        if word.startswith("re:"):
            try:
                matched = re.search(word[3:], raw_message, re.IGNORECASE) is not None
            except re.error:
                matched = False
        elif re.fullmatch(r"[A-Za-z0-9_ -]+", word):
            matched = re.search(r"(?<![A-Za-z0-9_])" + re.escape(word.lower()) + r"(?![A-Za-z0-9_])", lower) is not None
        else:
            matched = word.lower() in lower
        if matched:
            if bw.get("auto_delete", True) and message_id:
                try: await dispatcher.client.delete_msg(message_id)
                except Exception as error:
                    log.debug("Bad-word message deletion failed: %s", error)
            warn = str(bw.get("warn_msg", "请注意文明发言！"))
            # {user} must become a real at segment; a plain-text QQ number
            # does not render as a mention in QQ clients.
            segments = []
            for index, piece in enumerate(warn.replace("@{user}", "{user}").split("{user}")):
                if index:
                    segments.append({"type": "at", "data": {"qq": str(user_id)}})
                if piece:
                    segments.append({"type": "text", "data": {"text": piece}})
            await dispatcher.client.send_group_msg(group_id, segments or warn)
            log.info("Bad word filtered for user=%s", user_id)
            return True
    return False


async def handle_group_card(dispatcher, event):
    """群名片变更通知"""
    group_id = event.get("group_id", 0)
    user_id = event.get("user_id", 0)
    log.info("Group card changed: group=%s user=%s", group_id, user_id)


async def handle_group_msg_emoji_like(dispatcher, event):
    """群消息表情回应通知"""
    group_id = event.get("group_id", 0)
    user_id = event.get("user_id", 0)
    message_id = event.get("message_id", 0)
    likes = event.get("likes", [])
    if likes:
        emoji_desc = ", ".join(f"{e.get('emoji_id', '?')}x{e.get('count', 0)}" for e in likes[:5])
        log.debug("Emoji like on msg=%s in g=%s by u=%s: %s", message_id, group_id, user_id, emoji_desc)


async def handle_friend_add(dispatcher, event):
    """好友添加通知 — 将新好友加入缓存，不触发全量刷新"""
    user_id = event.get("user_id", 0)
    log.info("Friend added: u=%s", user_id)
    if hasattr(dispatcher, '_friend_cache'):
        dispatcher._friend_cache.add(int(user_id))
        dispatcher._friend_cache_ts = time.time() + 3600
        log.info("Friend cache updated: added u=%s", user_id)


async def handle_bot_offline(dispatcher, event):
    """机器人离线通知"""
    user_id = event.get("user_id", 0)
    tag = event.get("tag", "")
    log.warning("Bot offline notice: user=%s tag=%s", user_id, tag)


async def handle_title_change(dispatcher, event):
    """群头衔变更通知"""
    group_id = event.get("group_id", 0)
    user_id = event.get("user_id", 0)
    title = event.get("title", "")
    log.info("Title changed: group=%s user=%s", group_id, user_id)
    # Bot-initiated sets: the issuing command already replied, skip the
    # duplicate congrats (also avoids contradicting a timeout-then-success).
    pending = _bot_title_sets.pop((group_id, user_id), None)
    if pending and time.time() - pending[1] < 120 and pending[0] == title:
        return
    if group_id and title and user_id != dispatcher.config.get("bot_qq"):
        try:
            await dispatcher.client.send_group_msg(group_id,
                f"恭喜获得专属头衔「{title}」！")
        except Exception as error:
            log.debug("Title congratulation send failed: %s", error)


# (group_id, user_id) -> (title, ts): titles the bot set via /title or 我要头衔
_bot_title_sets = {}


def mark_title_set_by_bot(group_id, user_id, title):
    _bot_title_sets[(group_id, user_id)] = (title, time.time())
    if len(_bot_title_sets) > 200:
        cutoff = time.time() - 600
        for k in [k for k, v in _bot_title_sets.items() if v[1] < cutoff]:
            del _bot_title_sets[k]


async def handle_profile_like(dispatcher, event):
    """个人资料点赞通知 — 秒回点赞，SVIP点满20个，普通10个"""
    operator_id = event.get("operator_id", 0)
    from event_policy import automation_enabled
    if not automation_enabled(dispatcher.config, "like_back", True):
        return
    times = event.get("times", 0)
    log.info("Profile like received: operator=%s times=%s", operator_id, times)
    # 不回点机器人自己
    bot_qq = dispatcher.config.get("bot_qq")
    if operator_id == bot_qq:
        return
    # 短冷却 1 秒防并发重复事件（同一秒内可能收到多条重复通知）
    if not hasattr(dispatcher, '_last_like_back'):
        dispatcher._last_like_back = {}
    now = time.time()
    last = dispatcher._last_like_back.get(operator_id, 0)
    if now - last < 1:
        return
    dispatcher._last_like_back[operator_id] = now
    # SVIP 可点赞 20 次，普通用户 10 次。直接发 20，QQ 后端会自动封顶
    like_times = 20
    try:
        r = await dispatcher.client.send_like(operator_id, like_times)
        if r.get("status") == "ok":
            log.info("Profile like response sent: operator=%s times=%s", operator_id, like_times)
        else:
            msg = r.get("msg", "") or r.get("wording", "") or str(r)[:120]
            if r.get("retcode") == 1200 or "点赞数已达" in msg or "上限" in msg:
                log.info("Like back skipped for %s: daily limit reached", operator_id)
            else:
                log.warning("Like back x%s failed for %s: %s", like_times, operator_id, msg)
    except Exception as e:
        log.error("Like back error for %s: %s", operator_id, e)


async def handle_group_name_change(dispatcher, event):
    """群名变更通知"""
    group_id = event.get("group_id", 0)
    log.info("Group name changed: group=%s", group_id)
