"""AI tool gating and multi-round tool execution."""

import json
import logging
import re

from .providers import _call_deepseek, _call_deepseek_inner, _get_semaphore, _providers_support_tools

log = logging.getLogger("qqbot")

def _should_consider_napcat_tool(text):
    value = str(text or "").lower()
    keywords = (
        "群信息", "群资料", "群人数", "成员", "谁是", "群主", "管理员",
        "聊天记录", "历史消息", "刚才说", "群文件", "文件链接", "群公告",
        "群荣誉", "龙王", "禁言列表", "qq资料", "qq信息",
        "天气", "热榜", "热搜", "一言", "答案之书", "epic", "免费游戏",
        "精华", "翻译", "链接安全", "@全体", "全体", "壁纸",
        "表情回应", "贴表情", "点赞",
        "搜索", "查一下", "几点",
    )
    return any(keyword in value for keyword in keywords)

_READ_TOOL_SPEC = (
    "get_group_info 参数 group_id\n"
    "get_member_info 参数 group_id,user_id\n"
    "get_recent_messages 参数 group_id,count(1-20)\n"
    "get_group_files 参数 group_id,keyword\n"
    "get_group_notice 参数 group_id\n"
    "get_group_honor 参数 group_id,honor_type\n"
    "get_shut_list 参数 group_id\n"
    "get_friend_info 参数 user_id\n"
    "get_essence_list 参数 group_id\n"
    "get_group_info_ex 参数 group_id\n"
    "check_url_safely 参数 url\n"
    "translate_en2zh 参数 text\n"
    "get_group_at_all_remain 参数 group_id\n"
    "uapi_weather 参数 city (查真实天气)\n"
    "uapi_hotboard 参数 type(weibo/zhihu/bilibili/douyin/baidu/toutiao/ithome/github) (查热榜)\n"
    "uapi_saying 无参数 (随机一言)\n"
    "uapi_answerbook 参数 question (答案之书)\n"
    "uapi_epic_free 无参数 (Epic免费游戏)\n"
    "uapi_search 参数 query (联网搜索)\n"
)

_INTERACTION_TOOL_SPEC = (
    "set_msg_emoji_like 参数 message_id,emoji_id (给消息贴表情, message_id 用系统提供的当前消息id)\n"
    "send_like 参数 user_id,times(1-10) (给群友点赞)\n"
)

async def _maybe_call_napcat_tool(dispatcher, group_id, user_id, text, chat_context,
                                  interaction_allowed=False, message_id=0):
    """Bounded tool loop: at most 2 whitelisted calls before the final reply.

    Interaction tools (emoji reaction / like) are only offered in explicit
    or follow-up scenes, never for interjections, and are daily-capped.
    """
    tool_spec = _READ_TOOL_SPEC
    if interaction_allowed:
        tool_spec += _INTERACTION_TOOL_SPEC
    base_prompt = (
        "你负责选择是否调用工具。只输出一行JSON或NONE。\n"
        "可用工具：\n" + tool_spec +
        "当前群号和用户号由系统提供，不得编造。\n"
        "示例：{\"tool\":\"get_group_info\",\"arguments\":{}}\n"
        "如果无需工具只输出NONE。如需第二个工具，在看到第一个结果后再输出一行JSON。"
    )
    collected = []
    extra_context = ""
    from ai_tools import execute_tool, execute_interaction_tool, format_tool_result
    for _round in range(2):
        user_prompt = "当前群={} 当前用户={} 当前消息id={} 消息={}\n最近上下文={}{}".format(
            group_id, user_id, message_id, str(text)[:160],
            str(chat_context or "")[-500:], extra_context)
        decision = await _call_deepseek(
            dispatcher.config,
            [{"role": "system", "content": base_prompt},
             {"role": "user", "content": user_prompt}],
            max_tokens=80, temperature=0.1, session=dispatcher.client.session)
        if not decision or decision.strip().upper().startswith("NONE"):
            break
        try:
            match = re.search(r"\{.*\}", decision, re.S)
            payload = json.loads(match.group(0) if match else decision)
            name = payload.get("tool", "")
            args = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
            if group_id:
                args["group_id"] = group_id
            if name == "get_member_info" and not args.get("user_id"):
                args["user_id"] = user_id
            if name in ("set_msg_emoji_like", "send_like"):
                if not interaction_allowed:
                    break
                if name == "set_msg_emoji_like" and not args.get("message_id"):
                    args["message_id"] = message_id
                result = await execute_interaction_tool(
                    dispatcher, name, args, group_id=group_id or 0, user_id=user_id)
            else:
                result = await execute_tool(dispatcher, name, args)
            formatted = format_tool_result(result)
            collected.append(formatted)
            extra_context += "\n已执行工具结果=" + formatted
        except Exception as exc:
            log.debug("tool loop round ignored: %s", exc)
            break
    return "\n".join(collected)

async def _chat_with_tools(dispatcher, messages, tools, group_id, user_id,
                           message_id=0, interaction_allowed=False,
                           max_tokens=400, temperature=0.7):
    """Multi-round native function-calling loop (max 4 tool rounds).

    The whole loop runs inside a single AI semaphore hold — inner calls use
    _call_deepseek_inner directly and must NOT re-acquire the semaphore.
    Returns the final reply text, or None when tools are unsupported / the
    provider failed (caller then degrades to the legacy JSON loop).
    """
    from ai_tools import execute_ai_tool, format_tool_result
    config = dispatcher.config
    if not tools or not _providers_support_tools(config):
        return None
    runtime = config.get("runtime", {})
    async with _get_semaphore("ai", runtime.get("ai_concurrency", 1)):
        conversation = list(messages)
        for _round in range(4):
            message = await _call_deepseek_inner(
                config, conversation, max_tokens, temperature,
                dispatcher.client.session, tools=tools)
            if not isinstance(message, dict):
                return None  # provider failed or rejected tools -> legacy fallback
            tool_calls = message.get("tool_calls") or []
            content = (message.get("content") or "").strip()
            if not tool_calls:
                return content or None
            # Only the answered subset goes back into the conversation: an
            # assistant message carrying unanswered tool_call ids violates the
            # OpenAI chat schema and makes strict providers return 400.
            answered = tool_calls[:3]
            conversation.append({"role": "assistant", "content": content or None,
                                 "tool_calls": answered})
            for call in answered:
                fn = call.get("function", {}) if isinstance(call, dict) else {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except Exception:
                    args = {}
                result = await execute_ai_tool(
                    dispatcher, name, args, group_id=group_id or 0,
                    user_id=user_id or 0, message_id=message_id or 0,
                    interaction_allowed=interaction_allowed)
                conversation.append({"role": "tool",
                                     "tool_call_id": call.get("id", ""),
                                     "content": format_tool_result(result)})
            log.info("AI tool round %d: %d call(s) executed", _round + 1,
                     min(len(tool_calls), 3))
        # Rounds exhausted: force a final plain-text answer without tools.
        final = await _call_deepseek_inner(
            config, conversation, max_tokens, temperature,
            dispatcher.client.session)
        if isinstance(final, dict):
            return (final.get("content") or "").strip() or None
        return final
