"""Group, private, and self-message handling."""

import asyncio
import json
import logging
import os
import re
import time

from ..guard import add_blacklist, is_blacklisted
from ..permission import LEVEL_ADMIN, LEVEL_MASTER, get_group_config, get_user_level, is_group_enabled
from ..utils import atomic_write_json
from .context import (_cq_unescape, _event_scope_allowed, _log_chat_message, _read_tail_text, _service_state, _share_card_text)

log = logging.getLogger("qqbot")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class GroupMessageMixin:
    async def _handle_self_message(self, event):
        """Handle message_sent events — bot's own outgoing messages.

        Feeds them into the group/private message buffer so AI
        context includes what the bot itself just said.
        """
        msg_type = event.get("message_type", "")
        group_id = event.get("group_id", 0)
        user_id = event.get("user_id", 0)
        raw = event.get("raw_message", "") or ""
        message = event.get("message", [])
        sender = event.get("sender", {})
        sender_card = sender.get("card") or sender.get("nickname", "小汐")

        if msg_type == "group" and group_id and raw:
            if not is_group_enabled(self, group_id):
                return
            _log_chat_message(
                self, "GROUP_OUT", raw, group_id=group_id,
                user_id=self.config.get("bot_qq", 0), sender_name=sender_card,
            )
            bot_qq = self.config.get("bot_qq", 0)
            # Store in buffer so _build_chat_context sees it. Skip if ai.py just
            # appended the same reply (avoid duplicates from message_sent echo).
            now = time.time()
            buf = self._group_msg_buffer[group_id]
            if not (buf and buf[-1][0] == bot_qq and buf[-1][1][:60] == raw[:60]
                    and now - buf[-1][2] < 10):
                buf.append((bot_qq, raw, now, sender_card))
            # Dedup self-messages
            message_id = event.get("message_id", 0)
            if message_id:
                self._seen_msg_ids[message_id] = time.time()
            log.debug("[SELF] group=%s said: %s", group_id, raw[:60])
            # Fixed commands and title requests sent from the bot account itself
            await self._handle_self_group_command(group_id, raw, message, sender_card)

        elif msg_type == "private" and raw:
            peer_id = event.get("target_id") or user_id
            _log_chat_message(
                self, "PRIVATE_OUT", raw,
                user_id=peer_id, sender_name=sender_card,
            )
            log.debug("[SELF] private said: %s", raw[:60])
            # Commands typed from the bot account in the owner's chat window run
            # as owner commands; replies land in the same window.
            if peer_id == self.config.get("bot_owner"):
                prefix = self.config.get("command_prefix", "/")
                clean = re.sub(r"\[CQ:[^\]]+\]", "", raw).strip()
                if clean.startswith(prefix):
                    parts = clean[len(prefix):].split(maxsplit=1)
                    await self._handle_owner_command(
                        parts[0].lower(), parts[1] if len(parts) > 1 else "",
                        peer_id, {"nickname": sender_card}, message, raw,
                    )

    async def _handle_self_group_command(self, group_id, raw, message, sender_card):
        """Run fixed commands / title requests sent from the bot account itself.

        The bot account has master level (see permission.get_user_level), so
        permission checks stay centralized in _run_command.
        """
        prefix = self.config.get("command_prefix", "/")
        bot_qq = self.config.get("bot_qq", 0)
        clean = re.sub(r"\[CQ:[^\]]+\]", "", raw or "").strip()
        if clean.startswith(prefix):
            parts = clean[len(prefix):].split(maxsplit=1)
            cmd = parts[0].lower()
            if cmd in self.commands:
                log.info("[SELF] running command %s in group %s", cmd, group_id)
                await self._run_command(
                    cmd, parts[1] if len(parts) > 1 else "",
                    group_id, bot_qq, "owner", sender_card, message,
                )
            return
        from ..natural_triggers import extract_title_request
        title = extract_title_request(clean)
        if title:
            await self._run_command("mytitle", title, group_id, bot_qq, "owner",
                                    sender_card, message)

    async def _handle_message(self, event):
        msg_type = event.get("message_type", "")
        group_id = event.get("group_id", None)
        user_id = event.get("user_id", 0)

        # Defense in depth: keep direct callers from bypassing dispatch().
        if not _event_scope_allowed(self, event):
            return

        message = event.get("message", [])
        raw = event.get("raw_message", "") or ""
        sender = event.get("sender", {})
        message_id = event.get("message_id", 0)
        prefix = self.config.get("command_prefix", "/")

        # Deduplicate
        if message_id:
            async with self._lock:
                if message_id in self._seen_msg_ids:
                    return
                now_ts = time.time()
                self._seen_msg_ids[message_id] = now_ts
                if len(self._seen_msg_ids) > self._seen_msg_ids_maxlen:
                    # Evict entries older than 10 minutes to keep recent-only
                    cutoff_ts = time.time() - 600
                    for mid in list(self._seen_msg_ids.keys()):
                        if self._seen_msg_ids[mid] < cutoff_ts:
                            del self._seen_msg_ids[mid]

        # Sender role from NapCat (provided in real-time with each message)
        sender_role = sender.get("role", "member")
        sender_card = sender.get("card") or sender.get("nickname", str(user_id))

        # Group message handling. QQ share cards arrive with an empty
        # raw_message, so recover searchable text from json segments.
        if msg_type == "group" and not raw:
            card_text = _share_card_text(message)
            if ("b23.tv" in card_text or "bilibili.com/video" in card_text
                    or "BV1" in card_text):
                raw = card_text
        # NapCat may instead deliver cards inline as [CQ:json,data=...] in
        # raw_message; unescape so URL/BV detection sees the real links.
        if msg_type == "group" and "[CQ:json,data=" in raw:
            raw = _cq_unescape(raw).replace("\\/", "/")
        if msg_type == "group" and raw:
            group_enabled = is_group_enabled(self, group_id)
            if group_enabled:
                _log_chat_message(
                    self, "GROUP_IN", raw, group_id=group_id,
                    user_id=user_id, sender_name=sender_card,
                )
            # enable/disable are special - only bot_qq can use
            cmd_lower = raw.strip().lower()
            if cmd_lower == prefix + "enable" or cmd_lower == prefix + "disable" or \
               cmd_lower.startswith(prefix + "enable ") or cmd_lower.startswith(prefix + "disable "):
                from ..permission import get_user_level, LEVEL_MASTER
                caller_lvl, _ = await get_user_level(self, group_id, user_id, sender_role)
                bot_qq = self.config.get("bot_qq")
                if user_id == self.config.get("bot_owner") or user_id == bot_qq or caller_lvl >= LEVEL_MASTER:
                    parts = raw[len(prefix):].split(maxsplit=1)
                    await self._run_command(
                        parts[0].lower(), parts[1] if len(parts) > 1 else "",
                        group_id, user_id, sender_role, sender_card, message,
                    )
                else:
                    await self.client.send_group_msg(
                        group_id,
                        "这个只有群主人能开关"
                    )
                return

            if not group_enabled:
                return

            gcfg = get_group_config(self, group_id)
            feats = gcfg.get("features", {})

            log.debug("[RECV] group=%s user=%s card=%s role=%s raw=%s",
                      group_id, user_id, sender_card, sender_role, raw[:80])

            # Self-message: skip buffer + only process explicit commands
            is_self_msg = user_id == self.config.get("bot_qq")
            # URL safety check before recording message context.
            if not is_self_msg and raw:
                from ..security import check_message_urls
                if await check_message_urls(self, group_id, user_id, raw, message_id, sender_role):
                    return
            if not is_self_msg:
                # Message counting
                gc = self._group_msg_counts[group_id]
                gc[user_id] += 1
                self._group_msg_buffer[group_id].append((user_id, raw, time.time(), sender_card))
                self._record_human_turn(group_id, user_id, raw, message)
                self._message_stat_updates += 1
                self._state_dirty = True
                if self._message_stat_updates >= 30:
                    self._message_stat_updates = 0
                    self.save_runtime_state()

            # Collect stickers from image messages
            sticker_cfg = self.config.get("sticker_mode", {})
            if sticker_cfg.get("enabled", True) and sticker_cfg.get("collect", True):
                for seg in message:
                    if seg.get("type") == "image":
                        file_id = seg.get("data", {}).get("file", "")
                        sub_type = seg.get("data", {}).get("sub_type", "0")
                        summary = seg.get("data", {}).get("summary", "")
                        if file_id:
                            from ..ai import collect_sticker_async
                            self.create_background_task(
                                collect_sticker_async(self, group_id, file_id, sub_type, summary),
                                name="sticker-collect",
                            )

            # Bad word check
            from ..notice_handler import check_bad_words
            if await check_bad_words(self, group_id, user_id, raw, message_id):
                return

            # Repeat check
            if feats.get("repeat", True):
                if await self._check_repeat(group_id, raw, user_id):
                    return

            # Route to handler (skip for self-messages)
            if not is_self_msg:
                await self._handle_group_message(
                    group_id, user_id, message, raw, sender, sender_role, sender_card, message_id
                )
            else:
                # Self-message: only allow explicit commands
                import re as _re_self
                px = self.config.get("command_prefix", "/"); parts = raw[len(px):].split(maxsplit=1) if raw.startswith(px) else []
                if parts:
                    cmd = parts[0].lower()
                    if cmd in self.commands:
                        await self._run_command(cmd, parts[1] if len(parts) > 1 else "",
                                                group_id, user_id, sender_role, sender_card, message)

        elif msg_type == "private" and raw:
            _log_chat_message(
                self, "PRIVATE_IN", raw,
                user_id=user_id, sender_name=sender_card,
            )
            if user_id == self.config.get("bot_owner"):
                await self._handle_owner_private(user_id, message, raw, sender, message_id)
            else:
                # Non-owner private chat → AI auto-reply (no @ trigger needed)
                await self._handle_private_ai_chat(user_id, message, raw, sender, message_id)

    def _check_name_mention(self, raw_message):
        """Check if bot's name is mentioned in message (without @)"""
        nm_cfg = self.config.get("name_mention", {})
        if not nm_cfg.get("enabled", True):
            return False
        names = nm_cfg.get("names", ["小汐", "汐汐"])
        for name in names:
            if name in raw_message:
                return True
        return False

    def _check_followup(self, group_id, user_id):
        key = (group_id, user_id)
        last_ts = self._group_last_reply_to.get(key, 0)
        if time.time() - last_ts > 120:
            return False
        # Check if this user spoke after bot's last reply to them
        buffer = list(self._group_msg_buffer[group_id])
        if not buffer:
            return True  # No buffer = no one else spoke, assume followup
        # Count how many OTHER people spoke after bot replied
        others_spoke = 0
        for uid, raw, ts, card in reversed(buffer):
            if ts <= last_ts:
                break
            if uid != user_id:
                others_spoke += 1
        # Allow 1-2 other messages in between (someone might chip in briefly)
        return others_spoke <= 2

    async def _handle_group_message(self, group_id, user_id, message, raw, sender, sender_role, sender_card, message_id):
        prefix = self.config.get("command_prefix", "/")
        gcfg = get_group_config(self, group_id)
        feats = gcfg.get("features", {})
        is_at_bot = self._check_at_bot(message)
        is_name_mentioned = self._check_name_mention(raw) if not is_at_bot else False
        is_at_others = (not is_at_bot) and self._extract_mentions(message)

        # === BLACKLIST GUARD: check before all interactive features ===
        if is_blacklisted(group_id, user_id):
            log.info("Blocked blacklisted user %s in group %s", user_id, group_id)
            return

        # Strip CQ codes for command matching (e.g. [CQ:reply,id=xxx]/精华 → /精华)
        import re as _re_cmd
        clean_raw = _re_cmd.sub(r"\[CQ:[^\]]+\]", "", raw).strip()

        if clean_raw.startswith(prefix):
            parts = clean_raw[len(prefix):].split(maxsplit=1)
            cmd = parts[0].lower()
            await self._run_command(cmd, parts[1] if len(parts) > 1 else "",
                                    group_id, user_id, sender_role, sender_card, message)
            return

        # B站 video share: auto parse + download (feature-gated, 30s cooldown)
        bili_cfg = self.config.get("bilibili", {})
        if (bili_cfg.get("parse_enabled", True)
                and feats.get("bili_parse", True)
                and ("BV1" in raw or "b23.tv" in raw
                     or "bilibili.com/video" in raw)):
            from event_policy import allow_event
            if allow_event("bili_parse", group_id, 30):
                from ..bilibili import handle_share
                try:
                    if await handle_share(self, group_id, raw):
                        return
                except Exception as e:
                    log.warning("bili share handle failed: %s", e)

        # Galgame resource request: TouchGal metadata + official detail page.
        if feats.get("galgame_resource", True):
            from ..touchgal import handle_auto_request
            try:
                if await handle_auto_request(self, group_id, user_id, raw):
                    return
            except Exception as error:
                log.warning("TouchGal auto request failed: %s", error)

        # Music search
        if feats.get("music", True):
            # Also check natural music triggers
            from ..natural_triggers import is_music_trigger
            is_music, music_kw = is_music_trigger(raw)
            if is_music and music_kw:
                from ..commands import handle_music_search
                # Create fake raw text with standard prefix for the handler
                fake_raw = "我要点歌 " + music_kw
                if await handle_music_search(self, group_id, user_id, fake_raw, sender_card):
                    return
            else:
                from ..commands import handle_music_search
                if await handle_music_search(self, group_id, user_id, raw, sender_card):
                    return

        # === NATURAL LANGUAGE TRIGGERS ===
        from ..natural_triggers import check_natural_triggers
        trig = check_natural_triggers(raw, message)
        if trig:
            cmd_name, trig_args = trig
            if cmd_name == "kick":
                for target in trig_args.get("targets", []):
                    await self._run_command("kick", str(target), group_id, user_id, sender_role, sender_card, message)
            elif cmd_name == "ban":
                targets = trig_args.get("targets", [])
                duration = trig_args.get("args", "")
                for target in targets:
                    await self._run_command("ban", f"{duration} {target}".strip(), group_id, user_id, sender_role, sender_card, message)
            elif cmd_name == "unban":
                for target in trig_args.get("targets", []):
                    await self._run_command("unban", str(target), group_id, user_id, sender_role, sender_card, message)
            elif cmd_name == "mytitle":
                await self._run_command("mytitle", trig_args.get("title", ""),
                                        group_id, user_id, sender_role, sender_card, message)
            elif cmd_name in ("like", "fortune", "rank", "精华"):
                await self._run_command(cmd_name, "", group_id, user_id, sender_role, sender_card, message)
            return

        # AI-assisted admin intent: the target must come from a real @ segment.
        # The model only chooses the action/duration; permissions stay in code.
        from event_policy import automation_enabled
        if (is_at_others
                and automation_enabled(self.config, "ai_admin_intent", default=False)
                and await self._maybe_execute_admin_intent(
                    group_id, user_id, sender_role, raw, message)):
            return

        # === NEW AI CHAT LOGIC: hard filters + AI-driven judgment ===
        if not feats.get("ai_chat", True):
            return
        from ..ai import handle_ai_chat, search_web, _schedule_state
        is_explicit_trigger = is_at_bot or is_name_mentioned
        text = re.sub(r"\[CQ:[^\]]+\]", "", raw or "").strip()
        # Hard filters applied to every message
        if is_blacklisted(group_id, user_id):
            return
        if not self._check_global_rate_limit():
            if is_explicit_trigger:
                await self.client.send_group_msg(group_id, "今天回太多 让我歇会")
            return
        # Sleep hours: only explicit triggers wake the bot
        schedule_state, _ = _schedule_state()
        if schedule_state == "sleep" and not is_explicit_trigger:
            return
        # Explicit trigger (@bot / name mention): immediate reply
        if is_explicit_trigger:
            now = time.time()
            nm_cfg = self.config.get("name_mention", {})
            group_cd = nm_cfg.get("cooldown_seconds", 10)
            user_cd = nm_cfg.get("user_cooldown_seconds", 15)
            if now - self._group_last_at_bot.get(group_id, 0) < group_cd:
                return
            if now - self._user_last_name_reply.get(user_id, 0) < user_cd:
                return
            self._group_last_at_bot[group_id] = now
            self._user_last_name_reply[user_id] = now
            self._reset_consecutive_replies(group_id)
            allowed, remaining = self._check_rate_limit(group_id)
            if not allowed:
                await self.client.send_group_msg(group_id, "不行了不行了 刷屏太多 我潜一会 回头聊")
                return
            result = await self._do_ai_reply(
                group_id, user_id, raw, sender_card, message, message_id,
                reply_intent="直接回应",
                rate_warning=self._get_rate_limit_warning(remaining),
            )
            self._record_ai_outcome(group_id, bool(result))
            return
        # Follow-up window (user is replying to our recent message)
        is_followup = self._check_followup(group_id, user_id)
        if is_followup:
            allowed, remaining = self._check_rate_limit(group_id)
            if not allowed:
                return
            result = await self._do_ai_reply(
                group_id, user_id, raw, sender_card, message, message_id,
                reply_intent="继续闲聊",
            )
            self._record_ai_outcome(group_id, bool(result))
            return

        # Interjection candidate: cheap hard filter, then defer to delayed queue
        if not self._is_trivial_for_interjection(text, message):
            runtime = self.config.get("runtime", {})
            last_interject = self._group_interject_ts.get(group_id, 0)
            cooldown = runtime.get("non_explicit_judge_cooldown", 240)
            if time.time() - last_interject >= cooldown:
                max_consecutive = self.config.get("chat_limits", {}).get("max_consecutive_replies", 5)
                if self._group_consecutive_replies.get(group_id, 0) < max_consecutive:
                    await self._enqueue_delayed_reply(group_id, user_id, message_id, message, raw, sender_card)


class PrivateMessageMixin:
    async def _handle_owner_private(self, user_id, message, raw, sender, message_id):
        """Handle private messages from bot owner: commands first, then AI chat."""
        # Blacklist check
        from ..guard import is_blacklisted
        if is_blacklisted(0, user_id):
            return

        prefix = self.config.get("command_prefix", "/")

        # Check for command prefix first
        if raw.startswith(prefix):
            parts = raw[len(prefix):].split(maxsplit=1)
            cmd = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""
            await self._handle_owner_command(cmd, args, user_id, sender, message, raw)
            return

        # Non-command messages from owner → treat as normal AI chat
        await self._handle_private_ai_chat(user_id, message, raw, sender, message_id)

    async def _maybe_execute_admin_intent(self, group_id, actor_id, sender_role, raw, message):
        mentions = self._extract_mentions(message)
        if not mentions:
            return False
        text = re.sub(r"\[CQ:[^\]]+\]", "", raw or "").strip()
        if not any(word in text for word in ("踢", "禁言", "解禁", "闭嘴", "放出来")):
            return False
        from ..permission import get_user_level, LEVEL_ADMIN
        level, _ = await get_user_level(self, group_id, actor_id, sender_role)
        if actor_id != self.config.get("bot_owner") and level < LEVEL_ADMIN:
            return False
        from ..ai import _call_deepseek
        prompt = (
            "把管理员的QQ群管理语句解析为JSON。只允许 action=kick_member、ban_member、"
            "unban_member、none。duration为秒，默认禁言600秒，最大2592000秒。"
            "只输出JSON，不要解释。目标用户由系统提供，不要输出用户号。"
        )
        result = await _call_deepseek(
            self.config,
            [{"role": "system", "content": prompt}, {"role": "user", "content": text[:160]}],
            max_tokens=60, temperature=0.1, session=self.client.session)
        try:
            match = re.search(r"\{.*\}", result or "", re.S)
            payload = json.loads(match.group(0) if match else "{}")
            action = payload.get("action", "none")
            if action == "none":
                return False
            from ai_tools import execute_admin_tool
            tool_result = await execute_admin_tool(self, action, {
                "group_id": group_id, "user_id": mentions[0],
                "duration": payload.get("duration", 600),
            }, actor_id, sender_role)
            if tool_result.get("ok"):
                await self._reply(group_id, actor_id, "处理好了")
            else:
                await self._reply(group_id, actor_id, "没处理成：" + str(tool_result.get("error") or tool_result.get("message", "未知错误")))
            return True
        except Exception as exc:
            log.debug("Admin intent parse failed: %s", exc)
            return False

    async def _handle_owner_command(self, cmd, args, user_id, sender, message, raw):
        """Route owner private commands to handlers."""
        sender_name = sender.get("nickname", str(user_id))

        if cmd == "help":
            groups_list = ", ".join(str(g) for g in self.config.get("groups", {}).keys()) or "无"
            help_text = f"""小汐管理面板

群组: {groups_list}

/status - 查看状态
/AI状态 - 查看 SigmaI 和 DeepSeek 运行状态
/打卡状态 - 查看定时群打卡状态
/打卡测试 <群号> - 手动测试原生群打卡
/list - 查看所有群组数据概览
/log N - 查看最近N条日志 (默认30)
/chatlog N - 查看最近N条聊天日志 (默认30)
/bl list - 查看黑名单
/bl add <群号> <QQ> <小时> - 添加黑名单
/bl remove <群号> <QQ> - 移除黑名单
/group enable <群号> - 启用群
/group disable <群号> - 禁用群
/group list - 列出所有群
/memory <群号> - 查看该群的AI记忆
/memory clear <群号> - 清除该群的AI记忆
/sticker <群号> - 查看该群的表情包数量
/sticker clear <群号> - 清除该群的表情包
/sysmsg - 查看入群申请/邀请
/approve flag尾号 - 同意申请
/reject flag尾号 原因 - 拒绝申请
/health - 查看运行状态
/私聊AI on/off/allow/deny - 私聊AI开关与开放名单
/AI聊天 群号 on/off - 开关指定群的AI聊天
/安全 status/log - 查看安全功能和日志
/info <QQ号> - 查看任意人资料
/点赞信息 - 查看点赞统计
/积分 - 查看uapis积分额度
/b站推送 add 群号 mid - 盯UP主新投稿（mid=UP主空间网址 space.bilibili.com/ 后的数字，也可贴链接）
/全体 群号 内容 - @全体成员
/acg图 群号 on/off - 每日ACG图推送开关
/热榜推送 群号 on/off - 每日热榜推送开关
/b站解析 群号 on/off - B站自动解析开关
"""
            await self._reply(None, user_id, help_text)

        elif cmd in ("enable", "disable"):
            await self._run_command(cmd, args, None, user_id, "member", sender_name, message)

        elif cmd in ("ai状态", "aistatus"):
            from ..ai import format_ai_provider_status
            await self._reply(None, user_id, format_ai_provider_status(self.config))

        elif cmd in ("打卡状态", "checkinstatus"):
            from ..scheduler import format_checkin_status
            await self._reply(None, user_id, format_checkin_status(self))

        elif cmd in ("打卡测试", "checkintest"):
            gid = args.strip()
            if not gid.isdigit():
                await self._reply(None, user_id, "用法：/打卡测试 群号")
                return
            from ..scheduler import run_manual_checkin
            _ok, result_text = await run_manual_checkin(self, gid)
            await self._reply(None, user_id, result_text)

        elif cmd in ("私聊ai", "privateai"):
            await self._run_command("私聊ai", args, None, user_id, "member", sender_name, message)

        elif cmd in ("积分", "uapi"):
            await self._run_command("积分", args, None, user_id, "member", sender_name, message)

        elif cmd in self._private_group_command_names():
            target_group, rest_args = self._parse_private_group_args(args)
            if not target_group:
                await self._reply(None, user_id, "私聊跨群命令要带群号，比如 /{} 群号 参数".format(cmd))
                return
            await self._run_command(
                cmd, rest_args, target_group, user_id, "member", sender_name, message,
            )

        elif cmd in ("log", "chatlog", "聊天日志"):
            n = 30
            if args.strip():
                try:
                    n = int(args.strip())
                except Exception:
                    pass
            try:
                filename = "chat.log" if cmd in ("chatlog", "聊天日志") else "bot.log"
                log_path = os.path.join(_ROOT, filename)
                text = await asyncio.to_thread(
                    _read_tail_text, log_path, n, 65536, 4000 if filename == "chat.log" else 2000)
                await self._reply(None, user_id, text or "无日志")
            except Exception as e:
                await self._reply(None, user_id, f"读取日志失败: {e}")

        elif cmd == "bl":
            parts2 = args.split()
            if not parts2 or parts2[0] == "list":
                bl = self._load_guard_file(os.path.join(_ROOT, "data", "blacklist.json"))
                if not bl:
                    await self._reply(None, user_id, "黑名单为空")
                    return
                lines = []
                now = time.time()
                for key, entry in bl.items():
                    remaining = max(0, int(entry.get("expires", 0) - now) // 3600)
                    lines.append(f"  g{entry.get('group_id')} u{entry.get('user_id')} 剩余{remaining}h")
                await self._reply(None, user_id, "黑名单：\n" + "\n".join(lines[:30]))
            elif parts2[0] == "add" and len(parts2) >= 4:
                gid = parts2[1]
                uid = parts2[2]
                hours = 48
                try:
                    hours = int(parts2[3]) if len(parts2) > 3 else 48
                except Exception:
                    pass
                add_blacklist(gid, uid, hours, bot_owner=self.config.get("bot_owner"), bot_qq=self.config.get("bot_qq"))
                await self._reply(None, user_id, f"加进黑名单了：群 {gid}，QQ {uid}，{hours} 小时")
            elif parts2[0] == "remove" and len(parts2) >= 3:
                from ..guard import remove_blacklist
                remove_blacklist(parts2[1], parts2[2])
                await self._reply(None, user_id, f"移出黑名单了：群 {parts2[1]}，QQ {parts2[2]}")

        elif cmd == "status" or cmd == "state":
            try:
                bot_state, napcat_state = await asyncio.gather(
                    _service_state("qqbot.service"),
                    _service_state("napcat.service"),
                )
                def _cn_state(text):
                    value = (text or "").strip()
                    return {"active": "运行中", "inactive": "未运行", "failed": "异常", "activating": "启动中"}.get(value, value or "未知")
                try:
                    with open("/proc/uptime", encoding="utf-8") as f:
                        seconds = int(float(f.read().split()[0]))
                    uptime_text = f"运行时间：{seconds // 86400}天{seconds % 86400 // 3600}小时{seconds % 3600 // 60}分钟"
                except Exception:
                    uptime_text = "运行时间：未知"
                try:
                    meminfo = {}
                    with open("/proc/meminfo", encoding="utf-8") as f:
                        for line in f:
                            key, value = line.split(":", 1)
                            meminfo[key] = int(value.strip().split()[0])
                    total = meminfo.get("MemTotal", 0) // 1024
                    available = meminfo.get("MemAvailable", 0) // 1024
                    swap_total = meminfo.get("SwapTotal", 0) // 1024
                    swap_free = meminfo.get("SwapFree", 0) // 1024
                    mem_text = f"内存：可用 {available} 兆 / 总计 {total} 兆\n交换分区：可用 {swap_free} 兆 / 总计 {swap_total} 兆"
                except Exception:
                    mem_text = "内存：未知"
                status = f"NapCat：{_cn_state(napcat_state)}\n"
                status += f"小汐：{_cn_state(bot_state)}\n"
                status += mem_text + "\n"
                status += uptime_text
                await self._reply(None, user_id, status)
            except Exception as e:
                await self._reply(None, user_id, f"状态读取失败：{e}")

        elif cmd == "group" and args.strip():
            parts2 = args.split()
            if parts2[0] == "list":
                groups = self.config.get("groups", {})
                lines = []
                for gid, gcfg in groups.items():
                    st = "开启" if gcfg.get("enabled", True) else "关闭"
                    lines.append(f"  {gid} [{st}]")
                await self._reply(None, user_id, "群组:\n" + "\n".join(lines))
            elif parts2[0] in ("enable", "disable") and len(parts2) >= 2:
                gid = parts2[1]
                enabled = parts2[0] == "enable"
                with open(self._config_path, encoding="utf-8") as f:
                    cfg = json.load(f)
                if "groups" not in cfg:
                    cfg["groups"] = {}
                if gid not in cfg["groups"]:
                    cfg["groups"][gid] = json.loads(json.dumps(self.config.get("group_defaults", {})))
                cfg["groups"][gid]["enabled"] = enabled
                atomic_write_json(self._config_path, cfg, indent=2)
                self.config = cfg
                await self._reply(None, user_id, f"群 {gid} 已经{'开了' if enabled else '关了'}")

        elif cmd == "memory" and args.strip():
            parts2 = args.split()
            if parts2[0] == "clear" and len(parts2) >= 2:
                from ..ai import clear_group_memory
                clear_group_memory(self, parts2[1])
                await self._reply(None, user_id, f"群 {parts2[1]} 的记忆清掉了")
            else:
                from ..ai import _load_memory
                mem = _load_memory(parts2[0], self.config)
                if not mem:
                    await self._reply(None, user_id, f"群 {parts2[0]} 无记忆")
                else:
                    lines = []
                    for m in mem[-10:]:
                        role = "小汐" if m.get("role") == "assistant" else "群友"
                        content = (m.get("content") or "")[:80].replace("\n", " ")
                        lines.append(f"[{role}] {content}")
                    await self._reply(None, user_id, f"群 {parts2[0]} 最近记忆:\n" + "\n".join(lines))

        elif cmd == "sticker" and args.strip():
            parts2 = args.split()
            if parts2[0] == "clear" and len(parts2) >= 2:
                import os as _os
                sticker_path = _os.path.join(_ROOT, "data", "stickers", f"group_{parts2[1]}.json")
                if _os.path.exists(sticker_path):
                    _os.remove(sticker_path)
                    await self._reply(None, user_id, f"群 {parts2[1]} 表情包已清除")
                else:
                    await self._reply(None, user_id, f"群 {parts2[1]} 无表情包记录")
            else:
                import os as _os, json as _json
                sticker_path = _os.path.join(_ROOT, "data", "stickers", f"group_{parts2[0]}.json")
                if _os.path.exists(sticker_path):
                    with open(sticker_path) as _sf:
                        stickers = _json.load(_sf)
                    await self._reply(None, user_id, f"群 {parts2[0]} 共有 {len(stickers)} 个表情包")
                else:
                    await self._reply(None, user_id, f"群 {parts2[0]} 无表情包记录")

        elif cmd == "list":
            from ..commands import cmd_list
            await cmd_list(self, None, user_id, args, "member", sender_name, message)

        elif cmd == "sysmsg":
            from ..commands import cmd_sysmsg
            await cmd_sysmsg(self, None, user_id, args, "member", sender_name, message)

        elif cmd == "approve":
            from ..commands import cmd_approve_request
            await cmd_approve_request(self, None, user_id, args, "member", sender_name, message)

        elif cmd == "reject":
            from ..commands import cmd_reject_request
            await cmd_reject_request(self, None, user_id, args, "member", sender_name, message)

        elif cmd == "health":
            from ..commands import cmd_health
            await cmd_health(self, None, user_id, args, "member", sender_name, message)

        elif cmd == "安全":
            from ..commands import cmd_security
            await cmd_security(self, None, user_id, args, "member", sender_name, message)

        elif cmd == "clearai" and args.strip():
            gid = args.strip()
            import glob as _glob, os as _os2
            from ..ai import clear_group_memory
            from ..guard import load_blacklist, save_blacklist
            clear_group_memory(self, gid)
            sticker_path = _os2.path.join(_os2.path.dirname(_os2.path.dirname(_os2.path.abspath(__file__))),
                                        "data", "stickers", f"group_{gid}.json")
            if _os2.path.exists(sticker_path):
                _os2.remove(sticker_path)
            bl = load_blacklist()
            prefix_bl = f"{gid}_"
            removed = [k for k in bl if k.startswith(prefix_bl)]
            for k in removed:
                del bl[k]
            if removed:
                save_blacklist(bl)
            try:
                from ..guard import load_warnings, save_warnings
                w = load_warnings()
                removed_w = [k for k in w if k.startswith(prefix_bl)]
                for k in removed_w:
                    del w[k]
                if removed_w:
                    save_warnings(w)
            except Exception:
                pass
            user_mem_dir = _os2.path.join(_os2.path.dirname(_os2.path.dirname(_os2.path.abspath(__file__))),
                                        "data", "memories")
            pattern = _os2.path.join(user_mem_dir, f"group_{gid}_u*.json")
            removed_user_files = 0
            for f in _glob.glob(pattern):
                _os2.remove(f)
                removed_user_files += 1
            await self._reply(None, user_id, f"群 {gid} 的数据清掉了，包括记忆、表情包、黑名单和用户记忆")

        else:
            # Unknown command → just say so, don't trigger AI
            await self._reply(None, user_id, "未知命令，输入 /help 查看可用命令")

    async def _is_friend(self, user_id):
        """Check if user is a friend of the bot (lazy-load, no periodic refresh).

        Cache populated on first private message only. Never proactively
        refreshes — on low-spec servers with 800+ friends, periodic
        get_friend_list is wasteful.
        """
        now = time.time()
        if not hasattr(self, "_friend_cache"):
            self._friend_cache = set()
            self._friend_cache_ts = 0
        if self._friend_cache and now - self._friend_cache_ts < 3600:
            return user_id in self._friend_cache
        if now < self._friend_retry_after:
            return user_id in self._friend_cache

        async with self._friend_refresh_lock:
            now = time.time()
            if self._friend_cache and now - self._friend_cache_ts < 3600:
                return user_id in self._friend_cache
            if now < self._friend_retry_after:
                return user_id in self._friend_cache
            try:
                result = await self.client.call("get_friend_list", {})
                if result.get("status") == "ok":
                    friends = {
                        int(item.get("user_id", 0))
                        for item in result.get("data", [])
                        if item.get("user_id")
                    }
                    self._friend_cache = friends
                    self._friend_cache_ts = now
                    self._friend_retry_after = 0.0
                    log.info("Friend cache loaded on demand: %d friends", len(friends))
                    return user_id in friends
                log.warning("get_friend_list returned %s", result.get("status", "?"))
            except Exception as e:
                log.warning("get_friend_list failed: %s", e)

            self._friend_retry_after = now + 60
            if self._friend_cache:
                self._friend_cache_ts = now
                log.debug("Friend API failed, using stale cache (%d entries)", len(self._friend_cache))
                return user_id in self._friend_cache
            log.warning("Friend list never loaded, rejecting user %s until retry", user_id)
            return False

    async def _handle_private_ai_chat(self, user_id, message, raw, sender, message_id):
        """AI auto-reply for non-owner private chat. Friends only.

        Minimal code intervention — AI decides everything:
        - Whether to reply (output [SKIP] to skip)
        - What to reply
        - How long the reply should be
        - When to end the conversation

        Code only handles: blacklist, friend check, typing delay, sending.
        """
        import re as _re_priv

        sender_name = sender.get("nickname", str(user_id))

        # === Safety: blacklist ===
        from ..guard import is_blacklisted
        if is_blacklisted(0, user_id):
            log.debug("Private chat blocked (blacklisted): %s(%s)", sender_name, user_id)
            return

        # === Private AI gate: master switch + allowlist ===
        # Default OFF. Replies only when globally enabled, or the user is in
        # private_chat.allowed_users, or the user is the bot owner.
        pc_cfg = self.config.get("private_chat", {})
        pc_allowed_users = {int(u) for u in pc_cfg.get("allowed_users", [])
                            if str(u).isdigit()}
        is_allowed_user = user_id in pc_allowed_users
        if (user_id != self.config.get("bot_owner")
                and not pc_cfg.get("enabled", False)
                and not is_allowed_user):
            log.debug("Private AI disabled, ignoring: %s(%s)", sender_name, user_id)
            return

        # === Dedup: prevent concurrent AI calls for same user ===
        now = time.time()
        if user_id in self._private_processing:
            log.debug("Private dedup: user %s(%s) already processing, skipping", sender_name, user_id)
            return
        self._private_processing[user_id] = now
        typing_started = False

        try:
            # === Friend-only gate (silent): non-friends get no response at all ===
            if not await self._is_friend(user_id):
                if not is_allowed_user:
                    log.debug("Private chat skipped (not friend): %s(%s)", sender_name, user_id)
                    return

            # === Strip CQ codes for clean text ===
            clean_raw = _re_priv.sub(r"\[CQ:[^\]]+\]", "", raw).strip()
            has_image = any(seg.get("type") == "image" for seg in message if isinstance(seg, dict))

            # Truly empty (no text + no image) → skip even AI call
            if not clean_raw and not has_image:
                log.debug("Private chat skipped (empty): %s(%s)", sender_name, user_id)
                return

            # Show "typing..." while preparing (friend check / vision / search
            # can take seconds); handle_ai_chat keeps it on during generation.
            try:
                _tr = await self.client.call("set_input_status", {
                    "user_id": user_id, "event_type": 1,
                })
                typing_started = isinstance(_tr, dict) and _tr.get("status") == "ok"
            except Exception:
                pass

            # Build image context (only for non-sticker images)
            from ..media import extract_message_context
            img_ctx = await extract_message_context(self, None, message)
            if img_ctx:
                img_ctx = img_ctx[:300]

            # Search web for factual questions
            from ..ai import search_web
            search_text = clean_raw[:100]
            web_ctx = await search_web(self, search_text) if self._should_search_web(search_text) else ""

            # Call AI — it decides whether to reply and what to say
            from ..ai import handle_ai_chat
            consecutive = self._private_consecutive_replies.get(user_id, 0)
            log.info("Private AI evaluating: %s(%s) img=%s consec=%d",
                     sender_name, user_id, bool(img_ctx), consecutive)
            result = await handle_ai_chat(
                self, None, user_id, clean_raw, sender_name,
                image_context=img_ctx or "",
                message_id=message_id,
                web_search_results=web_ctx,
                reply_intent="直接回应",
                consecutive_replies=consecutive,
                interaction_allowed=True,
            )
            if result is True:
                log.info("Private AI replied to %s(%s)", sender_name, user_id)
                self._private_last_reply_ts[user_id] = time.time()
                self._private_consecutive_replies[user_id] = consecutive + 1
                self._private_urgent_pings.pop(user_id, None)
            elif result is None:
                log.debug("Private AI anti-echo skipped: %s(%s)", sender_name, user_id)
            else:
                log.debug("Private AI chose to skip: %s(%s) (consec=%d)", sender_name, user_id, consecutive)
                # Reset consecutive count when AI skips
                self._private_consecutive_replies.pop(user_id, None)
                # Reset after 10 min gap (handled by _cleanup_stale_state)
        finally:
            self._private_processing.pop(user_id, None)
            if typing_started:
                try:
                    await self.client.call("set_input_status", {
                        "user_id": user_id, "event_type": 0,
                    })
                except Exception:
                    pass

    def _parse_private_group_args(self, args):
        parts = (args or "").strip().split(maxsplit=1)
        if not parts:
            return 0, ""
        if not parts[0].isdigit():
            return 0, args
        return int(parts[0]), parts[1] if len(parts) > 1 else ""

    def _private_group_command_names(self):
        return {
            "kick", "ban", "unban", "allban", "welcome", "badword",
            "admin", "title", "头衔", "精华列表", "群荣誉",
            "群文件", "文件链接", "公告", "ocr", "转发摘要",
            "已读", "history", "禁言列表", "转发", "setgroupavatar", "全体", "acg图", "热榜推送", "b站解析", "b站推送", "ai聊天",
        }

    async def _get_image_context(self, group_id, message):
        """Return accurate image context. Cache hit → instant. Cache miss → wait for vision API."""
        import html as _html
        contexts = []
        for seg in message:
            if seg.get("type") != "image":
                continue
            data = seg.get("data", {}) if isinstance(seg.get("data"), dict) else {}
            file_id = data.get("file", "")
            summary = data.get("summary", "")
            sub_type = data.get("sub_type", "0")

            # Priority 1: cached vision API result (accurate, fast)
            cache = getattr(self, "_image_desc_cache", None)
            if cache and file_id in cache:
                cached = cache[file_id]
                desc = cached if isinstance(cached, str) else cached.get("desc", "")
                if desc:
                    contexts.append("图片：" + desc[:120])
                    continue

            # Priority 2: call vision API (blocks, but accurate)
            from ..ai import describe_image
            desc = await describe_image(self, group_id, file_id, sub_type, summary)
            if desc and desc not in ("[图片]", "[表情/贴纸]"):
                if not hasattr(self, "_image_desc_cache"):
                    self._image_desc_cache = {}
                self._image_desc_cache[file_id] = {"desc": desc, "ts": time.time()}
                contexts.append("图片：" + desc[:120])
            elif summary:
                # Priority 3: QQ summary as fallback when vision API fails
                contexts.append("图片：" + _html.unescape(summary)[:120])
            else:
                contexts.append("[图片]")
        return "\n".join(contexts) if contexts else ""
