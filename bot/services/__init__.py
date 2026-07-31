"""Long-running bot service boundaries."""

__all__ = [
    "DelayedReplyServiceMixin",
    "HealthServiceMixin",
    "MemberCacheMixin",
    "scheduler_loop",
]


def __getattr__(name):
    if name == "DelayedReplyServiceMixin":
        from .delayed_reply import DelayedReplyServiceMixin
        return DelayedReplyServiceMixin
    if name == "HealthServiceMixin":
        from .health import HealthServiceMixin
        return HealthServiceMixin
    if name == "MemberCacheMixin":
        from .member_cache import MemberCacheMixin
        return MemberCacheMixin
    if name == "scheduler_loop":
        from .scheduler import scheduler_loop
        return scheduler_loop
    raise AttributeError(name)
