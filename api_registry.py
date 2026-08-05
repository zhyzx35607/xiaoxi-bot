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
    min_version: str = "4.18.13"
    official_action: str = ""


_NAMES = {
    "message": (
        "send_msg", "send_group_msg", "send_private_msg", "delete_msg", "get_msg",
        "send_group_msg_reply", "send_group_msg_with_at",
        "get_group_msg_history", "mark_msg_as_read", "mark_group_msg_as_read",
        "mark_all_as_read", "set_input_status", "set_msg_emoji_like",
        "send_group_forward_msg", "send_private_forward_msg", "forward_group_single_msg",
        "forward_friend_single_msg", "send_forward_msg", "mark_private_msg_as_read",
        "_mark_all_as_read", "send_like", "fetch_emoji_like", "get_emoji_likes",
        "send_flash_msg", "click_inline_keyboard_button", "fetch_ptt_text",
    ),
    "group": (
        "get_group_list", "get_group_info", "get_group_info_ex", "get_group_member_list",
        "get_group_member_info", "get_group_member_list_cached", "get_group_honor_info",
        "get_group_shut_list", "get_group_at_all_remain", "get_essence_msg_list",
        "get_group_notice", "get_group_file_system_info", "get_group_root_files",
        "get_group_files_by_folder", "get_group_detail_info", "get_group_signed_list",
        "get_qun_album_list", "get_group_album_media_list",
    ),
    "management": (
        "set_group_kick", "set_group_ban", "set_group_whole_ban", "set_group_admin",
        "set_group_card", "set_group_special_title", "set_group_name", "set_group_portrait",
        "set_group_leave", "send_group_notice", "del_group_notice", "set_essence_msg",
        "delete_essence_msg", "set_group_add_request", "set_group_todo",
        "cancel_group_todo", "complete_group_todo", "upload_image_to_qun_album",
        "del_group_album_media", "do_group_album_comment", "set_group_album_media_like",
        "cancel_group_album_media_like", "set_group_remark", "set_group_search",
        "set_group_add_option", "set_group_robot_add_option", "set_group_kick_members",
    ),
    "file": (
        "get_group_file_url", "upload_group_file", "delete_group_file",
        "create_group_file_folder", "delete_group_folder", "move_group_file",
        "trans_group_file", "rename_group_file", "upload_private_file",
        "get_private_file_url", "download_file", "get_file", "get_online_file_msg",
        "send_online_file", "send_online_folder", "cancel_online_file",
        "receive_online_file", "refuse_online_file",
    ),
    "media": ("get_image", "ocr_image", "ocr_image_enhanced", "get_record", "get_forward_msg"),
    "friend": ("get_friend_list", "get_stranger_info", "get_profile_like", "friend_poke",
                "get_friend_msg_history", "get_friends_with_category", "get_recent_contact",
                "get_robot_uin_range", "get_unidirectional_friend_list",
                "get_doubt_friends_add_request", "set_doubt_friends_add_request",
                "set_friend_remark", "delete_friend"),
    "request": ("get_group_system_msg", "set_friend_add_request", "set_group_add_request"),
    "napcat": ("get_status", "get_version_info", "get_login_info", "can_send_image",
                "can_send_record", "check_url_safely", "translate_en2zh", "send_group_sign",
                "get_ai_characters", "get_ai_record", "send_group_ai_record",
                "ark_share_group", "ark_share_peer",
                "ArkShareGroup", "ArkSharePeer", "create_collection", "get_collection_list",
                "fetch_custom_face", "set_online_status", "set_qq_avatar", "set_self_longnick",
                "set_group_sign", "group_poke", "send_poke",
                "_send_group_notice", "_get_group_notice", "_del_group_notice", ".ocr_image",
                "set_qq_profile", "set_diy_online_status", "add_custom_face",
                "delete_custom_face", "set_custom_face_desc", "fetch_custom_face_detail"),
}

_INTERACTION = {
    "send_msg", "send_group_msg", "send_private_msg", "send_group_forward_msg",
    "send_private_forward_msg", "forward_group_single_msg", "forward_friend_single_msg",
    "send_forward_msg", "send_group_msg_reply", "send_group_msg_with_at", "send_flash_msg",
    "click_inline_keyboard_button", "set_msg_emoji_like", "friend_poke", "group_poke", "send_poke",
    "send_like", "send_group_sign", "set_group_sign", "send_group_ai_record",
    "mark_msg_as_read", "mark_group_msg_as_read", "mark_private_msg_as_read",
    "mark_all_as_read", "_mark_all_as_read", "set_input_status",
}
_MANAGEMENT = set(_NAMES["management"]) | {
    "delete_msg", "upload_group_file", "delete_group_file", "create_group_file_folder",
    "delete_group_folder", "move_group_file", "trans_group_file", "rename_group_file",
    "upload_private_file", "download_file", "set_friend_add_request", "set_online_status",
    "set_qq_avatar", "set_self_longnick", "create_collection", "set_group_todo",
    "cancel_group_todo", "complete_group_todo", "upload_image_to_qun_album",
    "del_group_album_media", "do_group_album_comment", "set_group_album_media_like",
    "cancel_group_album_media_like", "set_group_remark", "set_group_search",
    "set_group_add_option", "set_group_robot_add_option", "set_group_kick_members",
    "send_online_file", "send_online_folder", "cancel_online_file", "receive_online_file",
    "refuse_online_file", "set_doubt_friends_add_request", "set_friend_remark",
    "delete_friend", "set_qq_profile", "set_diy_online_status", "add_custom_face",
    "delete_custom_face", "set_custom_face_desc", "_send_group_notice", "_del_group_notice",
}


_AI_DENY = {
    "get_friend_list", "get_recent_contact", "get_friends_with_category",
    "get_group_system_msg", "get_group_ignore_add_request", "get_group_ignored_notifies",
    "get_doubt_friends_add_request", "get_online_file_msg", "get_collection_list",
    "get_unidirectional_friend_list", "fetch_custom_face_detail",
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
                ai_allowed=risk == "read" and name not in _AI_DENY,
                automation_allowed=(risk in {"read", "interaction"}
                                    and name not in _AI_DENY),
                official_action=name,
            )
    return registry


REGISTRY = build_registry()


def get_api_specs(category: Optional[str] = None):
    values = REGISTRY.values()
    if category:
        values = [s for s in values if s.category == category]
    return sorted(values, key=lambda s: (s.category, s.name))
