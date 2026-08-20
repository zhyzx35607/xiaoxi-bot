"""Centralized text response delivery with merged-forward support."""

import asyncio
import logging
import os
import re

from app.logging_setup import sanitize_log_message

from ..ai.reply import strip_command_prefix
from ..permission import (
    LEVEL_ADMIN,
    LEVEL_GOWNER,
    LEVEL_MASTER,
    LEVEL_SUPER,
    get_user_level,
)
from ..storage.runtime_paths import create_runtime_temp_file

log = logging.getLogger("qqbot")

# NapCat rejects forward nodes with oversized content; caller-supplied
# sections (e.g. help categories) bypass _split_sections, so re-split here.
_FORWARD_NODE_HARD_CHARS = 1000

# Degradation chain after merged-forward failure: sharded plain messages
# first, text-file upload only when those also fail. QQ single messages
# safely carry ~900 chars; shards are capped so one failure cannot spam.
_PLAIN_FALLBACK_SHARD_LIMIT = 10
_PLAIN_FALLBACK_MAX_CHARS = 900
_PLAIN_FALLBACK_INTERVAL = 0.5


def _write_text_file(path, text):
    with open(path, "w", encoding="utf-8") as output:
        output.write(str(text))


def _output_config(dispatcher):
    configured = dispatcher.config.get("message_output", {})
    return configured if isinstance(configured, dict) else {}


def _split_sections(text, target_chars=800):
    text = str(text or "").strip()
    if not text:
        return []
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
    sections = []
    current = ""
    for paragraph in paragraphs or [text]:
        candidate = paragraph if not current else current + "\n\n" + paragraph
        if len(candidate) <= target_chars:
            current = candidate
            continue
        if current:
            sections.append(current)
            current = ""
        remaining = paragraph
        while len(remaining) > target_chars:
            split_at = max(
                remaining.rfind("\n", 0, target_chars),
                remaining.rfind("。", 0, target_chars),
                remaining.rfind("；", 0, target_chars),
            )
            if split_at < target_chars // 2:
                split_at = target_chars
            else:
                split_at += 1
            sections.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].strip()
        current = remaining
    if current:
        sections.append(current)
    return sections


def build_forward_nodes(dispatcher, text, title="小汐整理的内容", sections=None):
    config = _output_config(dispatcher)
    target = max(300, int(config.get("forward_node_target_chars", 800) or 800))
    hard_cap = max(target, int(config.get("forward_node_hard_chars",
                                         _FORWARD_NODE_HARD_CHARS) or _FORWARD_NODE_HARD_CHARS))
    raw_sections = list(sections or _split_sections(text, target))
    body_sections = []
    for section in raw_sections:
        section = str(section)
        if len(section) <= hard_cap:
            body_sections.append(section)
        else:
            body_sections.extend(_split_sections(section, target))
    bot_qq = str(dispatcher.config.get("bot_qq", 0))
    nodes = [{
        "type": "node",
        "data": {"name": "小汐", "uin": bot_qq, "content": "【{}】".format(title)},
    }]
    for section in body_sections:
        nodes.append({
            "type": "node",
            "data": {"name": "小汐", "uin": bot_qq, "content": str(section)},
        })
    return nodes


def _fallback_notice(level, kind):
    if kind == "help":
        if level >= LEVEL_SUPER:
            return "主人，我都乖乖按你的权限分好啦，想用哪个直接叫我就好"
        if level >= LEVEL_MASTER:
            return "主人，这群能用的东西我都给你好好分完啦"
        if level >= LEVEL_GOWNER:
            return "群主，这群能用的功能都在上面了，慢慢看吧"
        if level >= LEVEL_ADMIN:
            return "管理相关的也给你归好了，别乱点就行"
        return "都塞这条里了，点开慢慢看吧"
    if level >= LEVEL_SUPER:
        return "主人，内容有点多，我乖乖给你收进上面那条啦"
    if level >= LEVEL_MASTER:
        return "主人，东西有点多，我给你好好整理到上面啦"
    if level >= LEVEL_GOWNER:
        return "群主，内容都归在上面那条了"
    if level >= LEVEL_ADMIN:
        return "有点长，我给你塞进上面那条了"
    return "太长了，给你塞转发里了，点开看吧"


async def _summarize(dispatcher, text, level, kind):
    if kind == "help":
        return _fallback_notice(level, kind)
    config = _output_config(dispatcher)
    if not config.get("ai_summary_enabled", True):
        return _fallback_notice(level, kind)
    max_chars = max(20, min(120, int(config.get("ai_summary_max_chars", 80) or 80)))
    timeout = max(1, min(10, float(config.get("ai_summary_timeout_seconds", 4) or 4)))
    address = "主人" if level >= LEVEL_MASTER else ("群主" if level >= LEVEL_GOWNER else "对方")
    persona = (
        "你是小汐。面对主人时温柔、顺从、可爱、亲近，不得慵懒敷衍；"
        if level >= LEVEL_MASTER else
        "你是小汐，安静、懒散、轻微高冷，但不是客服。"
    )
    prompt = (
        persona +
        "用一句不超过{}字的话告诉{}：下面内容大概是什么、最值得看什么。"
        "不要编造，不要说已为您整理，不要列点，不要加引号。\n\n{}"
    ).format(max_chars, address, str(text)[:6000])
    try:
        from ..ai.providers import _call_deepseek
        result = await asyncio.wait_for(
            _call_deepseek(
                dispatcher.config,
                [{"role": "user", "content": prompt}],
                max_tokens=100,
                temperature=0.4,
                session=getattr(dispatcher.client, "session", None),
            ),
            timeout=timeout,
        )
        result = re.sub(r"\s+", " ", str(result or "")).strip()
        if result:
            return result[:max_chars]
    except Exception as error:
        log.debug("forward summary failed: %s", error)
    return _fallback_notice(level, kind)


def _plain_fallback_chunks(sections):
    """Merge sections into plain messages; past the shard limit the tail is
    truncated with a notice instead of being dropped silently."""
    chunks = []
    current = ""
    for section in sections:
        section = str(section).strip()
        if not section:
            continue
        while len(section) > _PLAIN_FALLBACK_MAX_CHARS:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(section[:_PLAIN_FALLBACK_MAX_CHARS])
            section = section[_PLAIN_FALLBACK_MAX_CHARS:].strip()
        candidate = section if not current else current + "\n" + section
        if len(candidate) <= _PLAIN_FALLBACK_MAX_CHARS:
            current = candidate
        else:
            chunks.append(current)
            current = section
    if current:
        chunks.append(current)
    if len(chunks) > _PLAIN_FALLBACK_SHARD_LIMIT:
        dropped = len(chunks) - _PLAIN_FALLBACK_SHARD_LIMIT
        notice = f"\n（太长了，剩下{dropped}条塞不下了）"
        chunks = chunks[:_PLAIN_FALLBACK_SHARD_LIMIT]
        chunks[-1] = chunks[-1][:_PLAIN_FALLBACK_MAX_CHARS - len(notice)] + notice
        log.warning("plain fallback truncated: dropped=%d shards", dropped)
    return chunks


async def _send_plain_fallback(dispatcher, group_id, user_id, text, sections):
    """Last-resort plain messages before the text-file upload fallback."""
    config = _output_config(dispatcher)
    target = max(300, int(config.get("forward_node_target_chars", 800) or 800))
    chunks = _plain_fallback_chunks(sections or _split_sections(text, target))
    if not chunks:
        log.warning("plain fallback skipped: group=%s user=%s empty content",
                    group_id, user_id)
        return False
    for index, chunk in enumerate(chunks):
        if index:
            await asyncio.sleep(_PLAIN_FALLBACK_INTERVAL)
        # 帮助文本等含行首 "/命令" 的输出经 message_sent 回环会被当成主人命令
        # 执行，普通消息降级同样走 bot 输出侧的统一中和。
        chunk = strip_command_prefix(chunk)
        if group_id:
            result = await dispatcher.client.send_group_msg(group_id, chunk)
        else:
            result = await dispatcher.client.send_private_msg(user_id, chunk)
        if not (isinstance(result, dict) and result.get("status") == "ok"):
            log.warning("plain fallback send failed: group=%s user=%s chunk=%d/%d",
                        group_id, user_id, index + 1, len(chunks))
            return False
    return True


async def _upload_text_fallback(dispatcher, group_id, user_id, text, title):
    path = ""
    try:
        handle, path = create_runtime_temp_file("qqbot_", ".txt", world_readable=True)
        os.close(handle)
        await asyncio.to_thread(_write_text_file, path, text)
        name = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", title)[:40] + ".txt"
        if group_id:
            result = await dispatcher.client.upload_group_file(group_id, path, name)
        else:
            result = await dispatcher.client.upload_private_file(user_id, path, name)
        return isinstance(result, dict) and result.get("status") == "ok"
    except Exception as error:
        log.warning("long response file fallback failed: %s", error)
        return False
    finally:
        if path:
            try:
                await asyncio.to_thread(os.remove, path)
            except FileNotFoundError:
                pass
            except OSError:
                log.debug("Long-response temporary file cleanup failed")


async def send_text_response(dispatcher, group_id, user_id, text, *, force_forward=False,
                             kind="generic", title="小汐整理的内容", sections=None,
                             role_hint="", request_message_id=0):
    text = str(text or "")
    config = _output_config(dispatcher)
    threshold = max(50, int(config.get("forward_threshold_chars", 200) or 200))
    if not force_forward and len(text) <= threshold:
        if group_id:
            return await dispatcher.client.send_group_msg(group_id, text)
        return await dispatcher.client.send_private_msg(user_id, text)

    level, _ = await get_user_level(dispatcher, group_id, user_id, role_hint)
    nodes = build_forward_nodes(dispatcher, text, title=title, sections=sections)
    # QQ rejects merged forwards above roughly 100 nodes; chunk conservatively.
    max_nodes = max(10, min(80, int(config.get("forward_max_nodes", 50) or 50)))
    if len(nodes) > max_nodes:
        bot_qq = str(dispatcher.config.get("bot_qq", 0))
        body = nodes[1:]
        chunks = [body[index:index + max_nodes - 1]
                  for index in range(0, len(body), max_nodes - 1)]
        nodes_list = []
        for index, chunk in enumerate(chunks):
            chunk_title = title if len(chunks) == 1 else "{} ({}/{})".format(title, index + 1, len(chunks))
            nodes_list.append([{
                "type": "node",
                "data": {"name": "小汐", "uin": bot_qq, "content": "【{}】".format(chunk_title)},
            }] + chunk)
    else:
        nodes_list = [nodes]
    result = None
    first_message_id = 0
    for chunk_nodes in nodes_list:
        if group_id:
            result = await dispatcher.client.send_group_forward_msg(int(group_id), chunk_nodes)
        else:
            result = await dispatcher.client.send_private_forward_msg(int(user_id), chunk_nodes)
        if isinstance(result, dict) and result.get("status") == "ok":
            if not first_message_id:
                first_message_id = (result.get("data") or {}).get("message_id") or 0
            continue
        if isinstance(result, dict) and result.get("status") == "timeout" and group_id:
            # Forward may still have landed; verify via group history before retrying.
            await asyncio.sleep(15)
            history = await dispatcher.client.get_group_msg_history(int(group_id), count=20)
            if isinstance(history, dict) and title[:20] in str(history.get("data") or ""):
                continue
        log.warning(
            "forward send failed: group=%s user=%s nodes=%d status=%s retcode=%s wording=%s",
            group_id, user_id, len(chunk_nodes),
            result.get("status") if isinstance(result, dict) else result,
            result.get("retcode") if isinstance(result, dict) else None,
            sanitize_log_message(str(
                result.get("wording") or result.get("msg") or ""
            ), limit=200) if isinstance(result, dict) else "",
        )
        result = None
        break
    if result is None and nodes_list:
        result = await dispatcher.client.send_forward_msg(
            message_type="group" if group_id else "private",
            group_id=int(group_id) if group_id else None,
            user_id=int(user_id) if not group_id else None,
            messages=nodes,
        )
    if isinstance(result, dict) and result.get("status") == "ok":
        summary = strip_command_prefix(await _summarize(dispatcher, text, level, kind))
        message_id = first_message_id or (result.get("data") or {}).get("message_id")
        if group_id:
            segments = []
            reply_id = message_id or request_message_id
            if config.get("reply_to_forward", True) and reply_id:
                segments.append({"type": "reply", "data": {"id": str(reply_id)}})
            segments.append({"type": "at", "data": {"qq": str(user_id)}})
            segments.append({"type": "text", "data": {"text": " " + summary}})
            await dispatcher.client.send_group_msg(group_id, segments)
        elif message_id or request_message_id:
            reply_id = message_id or request_message_id
            await dispatcher.client.send_private_msg(user_id, [
                {"type": "reply", "data": {"id": str(reply_id)}},
                {"type": "text", "data": {"text": summary}},
            ])
        return result

    if await _send_plain_fallback(dispatcher, group_id, user_id, text, sections):
        return {"status": "ok", "fallback": "plain_messages"}
    if await _upload_text_fallback(dispatcher, group_id, user_id, text, title):
        fallback = "太长了，转发没发出去，我放到文本文件里了"
    else:
        fallback = "内容太长，但转发和文件都没发出去，等会再试吧"
    if group_id:
        return await dispatcher.client.send_group_msg(group_id, fallback)
    return await dispatcher.client.send_private_msg(user_id, fallback)
