"""Small, serializable contracts shared by the Agent layer."""

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Mapping

class IdentityLevel(IntEnum):
    MEMBER = 1
    GROUP_ADMIN = 2
    GROUP_OWNER = 3
    SUPER_OWNER = 5

@dataclass(frozen=True)
class AgentScope:
    kind: str
    key: str
    group_id: int | None = None
    owner_id: int | None = None
    @property
    def is_private(self):
        return self.kind == "private"

@dataclass(frozen=True)
class AgentIdentity:
    user_id: int
    level: IdentityLevel
    role: str = "member"
    is_bot: bool = False
    @property
    def can_manage_agent(self):
        return self.level >= IdentityLevel.GROUP_OWNER
    @property
    def is_super_owner(self):
        return self.level >= IdentityLevel.SUPER_OWNER

@dataclass(frozen=True)
class AgentEvent:
    event_id: str
    scope: AgentScope
    identity: AgentIdentity
    text: str
    raw_message: str = ""
    message_type: str = ""
    message_id: int | str | None = None
    timestamp: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class MemoryCandidate:
    scope_key: str
    subject_id: int | None
    content: str
    confidence: float
    requires_confirmation: bool = True
    source_event_id: str = ""

@dataclass(frozen=True)
class AgentDecision:
    should_reply: bool
    reason: str
    requires_confirmation: bool = False
    tool_names: tuple[str, ...] = ()
