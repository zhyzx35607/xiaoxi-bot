# bot/notice_handler.py - Group notices, poke, badwords, admin changes
import json, logging, time, re, os
from .permission import get_group_config, is_group_enabled
from .utils import atomic_write_json

log = logging.getLogger("qqbot")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GROUP_FILES_PATH = os.path.join(_ROOT, "data", "group_files.json")


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
            from .security import handle_gray_tip
            await handle_gray_tip(dispatcher, event)
        else:
            log.info("Notify event subtype=%s group=%s user=%s target=%s raw=%s",
                     sub, event.get("group_id"), event.get("user_id"),
                     event.get("target_id"), str(event)[:300])
    else:
        log.info("Unhandled notice type=%s raw=%s", notice_type, str(event)[:300])


async def handle_group_increase(dispatcher, event):
    group_id = event.get("group_id", 0)
    if not is_group_enabled(dispatcher, group_id):
        return
    user_id = event.get("user_id", 0)
    gcfg = get_group_config(dispatcher, group_id)
    wm = gcfg.get("welcome_msg", {})
    if not wm.get("enabled", True):
        return
    template = wm.get("template", "欢迎 {nickname} 加入本群！")
    try:
        info = await dispatcher.client.get_group_member_info(group_id, user_id)
        nickname = str(user_id)
        if info.get("status") == "ok":
            data = info.get("data", {})
            nickname = data.get("card") or data.get("nickname", str(user_id))
    except Exception:
        nickname = str(user_id)
    msg = template.replace("{nickname}", nickname).replace("{user_id}", str(user_id))
    await dispatcher.client.send_group_msg(group_id, msg)


async def handle_group_decrease(dispatcher, event):
    group_id = event.get("group_id", 0)
    if not is_group_enabled(dispatcher, group_id):
        return
    user_id = event.get("user_id", 0)
    sub_type = event.get("sub_type", "")

    if sub_type == "kick_me":
        log.info("Bot kicked from group %s", group_id)
        with open(dispatcher._config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        groups = cfg.get("groups", {})
        gid = str(group_id)
        if gid in groups:
            groups[gid]["enabled"] = False
            atomic_write_json(dispatcher._config_path, cfg, indent=2)
            dispatcher.config = cfg
        return

    # Resolve nickname — API may fail since user already left
    nickname = ""
    try:
        info = await dispatcher.client.get_group_member_info(group_id, user_id)
        if info.get("status") == "ok":
            data = info.get("data", {})
            nickname = data.get("card") or data.get("nickname", "")
    except Exception:
        pass

    # Fallback: member cache (reverse lookup)
    if not nickname:
        cache = getattr(dispatcher, '_group_member_cache', {}).get(group_id, {})
        for name, qq in cache.items():
            if qq == user_id:
                nickname = name
                break

    # Fallback: recent message buffer
    if not nickname:
        buffer = list(dispatcher._group_msg_buffer.get(group_id, []))
        for uid, _, _, card in reversed(buffer):
            if uid == user_id and card:
                nickname = card
                break

    if not nickname:
        nickname = str(user_id)

    action = "被移出" if sub_type == "kick" else "离开了"
    text = f"{nickname} {action}群聊" if nickname != str(user_id) else f"{nickname}({user_id}) {action}群聊"
    await dispatcher.client.send_group_msg(group_id, text)


async def handle_group_admin(dispatcher, event):
    """Monitor admin changes - currently logs but bot role is always queried in real-time."""
    group_id = event.get("group_id", 0)
    user_id = event.get("user_id", 0)
    sub_type = event.get("sub_type", "")
    bot_qq = dispatcher.config["bot_qq"]

    if user_id == bot_qq:
        new_role = "admin" if sub_type == "set" else "member"
        log.info("Bot admin status changed: g=%s role=%s", group_id, new_role)
        # Bot role is queried in real-time on each command, so no cache to update
        # Just log for awareness


async def handle_group_recall(dispatcher, event):
    """Group message recall notice - log for now."""
    group_id = event.get("group_id", 0)
    operator_id = event.get("operator_id", 0)
    message_id = event.get("message_id", 0)
    log.debug("Message recalled in g=%s by %s mid=%s", group_id, operator_id, message_id)


async def handle_group_upload(dispatcher, event):
    group_id = event.get("group_id", 0)
    user_id = event.get("user_id", 0)
    file_info = event.get("file", {}) or {}
    name = file_info.get("name") or file_info.get("file_name") or file_info.get("id") or "未知文件"
    file_id = file_info.get("id") or file_info.get("file_id") or ""
    busid = file_info.get("busid") or file_info.get("bus_id") or ""
    size = file_info.get("size") or file_info.get("file_size") or 0
    log.info("Group file uploaded: g=%s u=%s name=%s id=%s busid=%s size=%s",
             group_id, user_id, name, file_id, busid, size)
    try:
        try:
            with open(_GROUP_FILES_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        files = data.setdefault(str(group_id), [])
        files.append({
            "ts": time.time(),
            "user_id": user_id,
            "name": name,
            "file_id": file_id,
            "busid": busid,
            "size": size,
        })
        data[str(group_id)] = files[-200:]
        atomic_write_json(_GROUP_FILES_PATH, data, indent=2)
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
    user_id = event.get("user_id", 0)
    target_id = event.get("target_id", 0)
    bot_qq = dispatcher.config["bot_qq"]

    if target_id != bot_qq:
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
                except Exception: pass
            warn = bw.get("warn_msg", "请注意文明发言！").replace("{user}", str(user_id))
            await dispatcher.client.send_group_msg(group_id, warn)
            log.info("Bad word filtered: %s from %s", word, user_id)
            return True
    return False


async def handle_group_card(dispatcher, event):
    """群名片变更通知"""
    group_id = event.get("group_id", 0)
    user_id = event.get("user_id", 0)
    card_new = event.get("card_new", "")
    card_old = event.get("card_old", "")
    log.info("Group card changed: g=%s u=%s old=%s new=%s", group_id, user_id, card_old, card_new)


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
    """好友添加通知"""
    user_id = event.get("user_id", 0)
    log.info("Friend added: u=%s", user_id)


async def handle_bot_offline(dispatcher, event):
    """机器人离线通知"""
    user_id = event.get("user_id", 0)
    tag = event.get("tag", "")
    message = event.get("message", "")
    log.warning("Bot offline: u=%s tag=%s msg=%s", user_id, tag, message)


async def handle_title_change(dispatcher, event):
    """群头衔变更通知"""
    group_id = event.get("group_id", 0)
    user_id = event.get("user_id", 0)
    title = event.get("title", "")
    log.info("Title changed: g=%s u=%s title=%s", group_id, user_id, title)
    if group_id and title and user_id != dispatcher.config.get("bot_qq"):
        try:
            await dispatcher.client.send_group_msg(group_id,
                f"恭喜获得专属头衔「{title}」！")
        except Exception:
            pass


async def handle_profile_like(dispatcher, event):
    """个人资料点赞通知 — 秒回点赞，SVIP点满20个，普通10个"""
    operator_id = event.get("operator_id", 0)
    operator_nick = event.get("operator_nick", "")
    times = event.get("times", 0)
    log.info("Profile like: operator=%s(%s) times=%s", operator_id, operator_nick, times)
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
            log.info("Liked back %s(%s) x%s", operator_nick, operator_id, like_times)
        else:
            # 如果不是 SVIP 被拒绝，回退到 10
            log.warning("Like back x%s failed for %s: %s, retrying x10",
                       like_times, operator_id, r.get("msg", "") or str(r)[:80])
            await dispatcher.client.send_like(operator_id, 10)
    except Exception as e:
        log.error("Like back error for %s: %s", operator_id, e)


async def handle_group_name_change(dispatcher, event):
    """群名变更通知"""
    group_id = event.get("group_id", 0)
    name_new = event.get("name_new", "")
    log.info("Group name changed: g=%s new=%s", group_id, name_new)
