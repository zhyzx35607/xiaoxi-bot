import logging
import re

from .memory import _is_repetitive


log = logging.getLogger(__name__)

_CLOSING_PATTERNS = (
    "有需要随时找我", "需要的话随时找我", "有事随时找我", "有事就喊我",
    "随时都可以找我", "我会一直在", "我一直都在",
)


def is_template_closing(reply):
    text = re.sub(r"[\s，。！？!?,.~～😊🙂]+", "", str(reply or "")).lower()
    return len(text) <= 32 and any(pattern in text for pattern in _CLOSING_PATTERNS)


def should_suppress_reply(
    dispatcher, user_id, group_id, is_owner_tier, is_super_owner, reply
):
    if not user_id:
        return False
    if is_super_owner and not group_id:
        if is_template_closing(reply):
            log.info("Owner template closing suppressed")
            return True
        checker = getattr(dispatcher, "_owner_reply_is_repetitive", None)
        if callable(checker) and checker(reply):
            log.info("Owner anti-echo skipped repetitive reply")
            return True
    elif not is_owner_tier and _is_repetitive(
            user_id, reply, scope=str(group_id) if group_id else "private_{}".format(user_id)):
        log.info("Anti-echo skipped repetitive reply for user=%s", user_id)
        return True
    return False


def observe_owner_reply(dispatcher, reply, topic):
    try:
        recorder = getattr(dispatcher, "_record_owner_reply", None)
        if callable(recorder):
            recorder(reply)
        companion = getattr(getattr(dispatcher, "agent_runtime", None), "companion", None)
        if companion is not None:
            companion.observe_outgoing(reply, topic=topic or "conversation")
    except Exception as error:
        log.debug("Companion outgoing observation failed: %s", error)
