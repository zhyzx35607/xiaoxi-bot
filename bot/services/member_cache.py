"""Group member nickname cache service."""

import logging
import time

log = logging.getLogger("qqbot")

class MemberCacheMixin:
    async def _refresh_member_cache(self, group_id):
        """Build nickname->QQ cache from recent message buffer (zero API calls).
        Only active speakers are cached — silent members don't need @-resolution."""
        try:
            group_id = int(group_id)
        except (TypeError, ValueError):
            log.warning("Member cache skipped: non-numeric group id %r", group_id)
            return
        now = time.time()
        if group_id in self._member_cache_ts and now - self._member_cache_ts.get(group_id, 0) < 600:
            return
        cache = {}
        buffer = self._group_msg_buffer.get(group_id, [])
        for user_id, _raw, _ts, sender_card in buffer:
            if sender_card and user_id:
                cache[sender_card] = user_id
        if cache:
            self._group_member_cache[group_id] = cache
            self._member_cache_ts[group_id] = now
            log.debug("Member cache from buffer for group %s: %d speakers", group_id, len(cache))
