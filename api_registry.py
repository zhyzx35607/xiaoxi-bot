"""NapCat/OneBot capability registry and safe API metadata."""

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class ApiSpec:
    name: str
    category: str
    scope: str = "both"
    risk: str = "read"
    ai_allowed: bool = False
    automation_allowed: bool = False
    timeout: float = 6.0


_NAMES = {
    "message": (
        "send_msg", "send_group_msg", "send_private_msg", "delete_msg", "get_msg",
        "get_group_msg_history", "mark_msg_as_read", "mark_group_msg_as_read",
        "mark_all_as_read", "set_input_status", "set_msg_emoji_like",
        "send_group_forward_msg", "send_private_forward_msg", "forward_group_single_msg",
        "forward_friend_single_msg", "send_forward_msg", "mark_private_msg_as_read",
        "_mark_all_as_read", "send_like",
    ),
    "group": (
        "get_group_list", "get_group_info", "get_group_info_ex", "get_group_member_list",
        "get_group_member_info", "get_group_member_list_cached", "get_group_honor_info",
        "get_group_shut_list", "get_group_at_all_remain", "get_essence_msg_list",
        "get_group_notice", "get_group_file_system_info", "get_group_root_files",
        "get_group_files_by_folder",
    ),
    "management": (
        "set_group_kick", "set_group_ban", "set_group_whole_ban", "set_group_admin",
        "set_group_card", "set_group_special_title", "set_group_name", "set_group_portrait",
        "set_group_leave", "send_group_notice", "del_group_notice", "set_essence_msg",
        "delete_essence_msg", "set_group_add_request",
    ),
    "file": (
        "get_group_file_url", "upload_group_file", "delete_group_file",
        "create_group_file_folder", "delete_group_folder", "move_group_file",
        "trans_group_file", "rename_group_file", "upload_private_file",
        "get_private_file_url", "download_file", "get_file",
    ),
    "media": ("get_image", "ocr_image", "ocr_image_enhanced", "get_record", "get_forward_msg"),
    "friend": ("get_friend_list", "get_stranger_info", "get_profile_like", "friend_poke",
                "get_friend_msg_history", "get_friends_with_category", "get_recent_contact",
                "get_robot_uin_range"),
    "request": ("get_group_system_msg", "set_friend_add_request", "set_group_add_request"),
    "napcat": ("check_url_safely", "translate_en2zh", "send_group_sign", "get_ai_characters", "get_ai_record", "send_group_ai_record",
                "ArkShareGroup", "ArkSharePeer", "create_collection", "get_collection_list",
                "fetch_custom_face", "set_online_status", "set_qq_avatar", "set_self_longnick",
                "set_group_sign", "group_poke", "send_poke",
                "_send_group_notice", "_get_group_notice", "_del_group_notice", ".ocr_image"),
}

_INTERACTION = {
    "send_msg", "send_group_msg", "send_private_msg", "send_group_forward_msg",
    "send_private_forward_msg", "forward_group_single_msg", "forward_friend_single_msg",
    "send_forward_msg", "set_msg_emoji_like", "friend_poke", "group_poke", "send_poke",
    "send_like", "send_group_sign", "set_group_sign", "send_group_ai_record",
    "mark_msg_as_read", "mark_group_msg_as_read", "mark_private_msg_as_read",
    "mark_all_as_read", "_mark_all_as_read", "set_input_status",
}
_MANAGEMENT = set(_NAMES["management"]) | {
    "delete_msg", "upload_group_file", "delete_group_file", "create_group_file_folder",
    "delete_group_folder", "move_group_file", "trans_group_file", "rename_group_file",
    "upload_private_file", "download_file", "set_friend_add_request", "set_online_status",
    "set_qq_avatar", "set_self_longnick", "create_collection",
}


def build_registry() -> Dict[str, ApiSpec]:
    registry: Dict[str, ApiSpec] = {}
    for category, names in _NAMES.items():
        for name in names:
            risk = "management" if name in _MANAGEMENT else ("interaction" if name in _INTERACTION else "read")
            registry[name] = ApiSpec(
                name=name, category=category,
                scope="private" if "private" in name or name in {"get_friend_list", "friend_poke"} else "both",
                risk=risk,
                ai_allowed=risk == "read",
                automation_allowed=risk in {"read", "interaction"},
            )
    return registry


REGISTRY = build_registry()


def get_api_specs(category: Optional[str] = None):
    values = REGISTRY.values()
    if category:
        values = [s for s in values if s.category == category]
    return sorted(values, key=lambda s: (s.category, s.name))
