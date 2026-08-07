"""Owner-private roleplay commands."""

from __future__ import annotations

import asyncio
import logging


log = logging.getLogger("qqbot")


def _service(dispatcher):
    service = getattr(dispatcher, "roleplay", None)
    if service is None:
        raise RuntimeError("角色扮演服务未初始化")
    return service


async def _run(dispatcher, group_id, user_id, fn, *args, **kwargs):
    try:
        result = (
            await fn(*args, **kwargs)
            if asyncio.iscoroutinefunction(fn)
            else await asyncio.to_thread(fn, *args, **kwargs)
        )
    except PermissionError as exc:
        result = str(exc)
    except Exception as exc:
        log.warning(
            "Roleplay command failed function=%s error=%s",
            getattr(fn, "__name__", "unknown"), exc,
        )
        result = "操作失败：请检查参数或文件格式"
    await dispatcher._reply(group_id, user_id, result)


async def cmd_char(dispatcher, group_id, user_id, args, role, sender_card, message):
    service = _service(dispatcher)
    parts = args.strip().split(maxsplit=1)
    action = parts[0].lower() if parts else "list"
    value = parts[1].strip() if len(parts) > 1 else ""
    if action == "list":
        return await _run(dispatcher, group_id, user_id, service.list_characters, user_id, group_id)
    if action == "import":
        return await _run(dispatcher, group_id, user_id, service.import_character, user_id, group_id, value)
    if action == "show":
        return await _run(dispatcher, group_id, user_id, service.show_character, user_id, group_id, value)
    if action == "export":
        return await _run(dispatcher, group_id, user_id, service.export_character, user_id, group_id, value)
    if action == "delete":
        target, separator, confirmation = value.rpartition("|")
        if not separator:
            target, confirmation = value, ""
        return await _run(dispatcher, group_id, user_id, service.delete_character, user_id, group_id, target.strip(), confirmation.strip())
    await dispatcher._reply(group_id, user_id, "用法：/char list | import <导入目录内文件> | show <角色> | export <角色> | delete <角色> | 确认")


async def cmd_persona(dispatcher, group_id, user_id, args, role, sender_card, message):
    service = _service(dispatcher)
    parts = args.strip().split(maxsplit=1)
    action = parts[0].lower() if parts else "list"
    value = parts[1].strip() if len(parts) > 1 else ""
    if action == "list":
        return await _run(dispatcher, group_id, user_id, service.list_personas, user_id, group_id)
    if action == "use":
        return await _run(dispatcher, group_id, user_id, service.use_persona, user_id, group_id, value)
    if action == "delete":
        return await _run(dispatcher, group_id, user_id, service.delete_persona, user_id, group_id, value)
    if action == "create":
        name, sep, description = value.partition("|")
        if not sep:
            await dispatcher._reply(group_id, user_id, "用法：/persona create <名称> | <描述>")
            return
        return await _run(dispatcher, group_id, user_id, service.create_persona, user_id, group_id, name.strip(), description.strip())
    await dispatcher._reply(group_id, user_id, "用法：/persona list | create <名称> | <描述> | use <名称> | delete <名称>")


async def cmd_chat(dispatcher, group_id, user_id, args, role, sender_card, message):
    service = _service(dispatcher)
    parts = args.strip().split(maxsplit=1)
    action = parts[0].lower() if parts else "current"
    value = parts[1].strip() if len(parts) > 1 else ""
    if action in {"current", "status"}:
        return await _run(dispatcher, group_id, user_id, service.status, user_id, group_id)
    if action == "list":
        return await _run(dispatcher, group_id, user_id, service.list_chats, user_id, group_id)
    if action == "new":
        character, sep, title = value.partition("|")
        return await _run(dispatcher, group_id, user_id, service.new_chat, user_id, group_id, character.strip(), title.strip() if sep else "")
    if action == "use":
        return await _run(dispatcher, group_id, user_id, service.use_chat, user_id, group_id, value)
    if action == "rename":
        return await _run(dispatcher, group_id, user_id, service.rename_chat, user_id, group_id, value)
    if action == "delete":
        return await _run(dispatcher, group_id, user_id, service.archive_chat, user_id, group_id, value)
    if action == "export":
        return await _run(dispatcher, group_id, user_id, service.export_chat, user_id, group_id)
    await dispatcher._reply(group_id, user_id, "用法：/chat list | new <角色> [| 标题] | use <编号> | rename <标题> | export | delete 确认")


async def cmd_memory(dispatcher, group_id, user_id, args, role, sender_card, message):
    service = _service(dispatcher)
    parts = args.strip().split(maxsplit=1)
    action = parts[0].lower() if parts else "list"
    value = parts[1].strip() if len(parts) > 1 else ""
    if action == "list":
        return await _run(dispatcher, group_id, user_id, service.list_memories, user_id, group_id, "")
    if action == "search":
        return await _run(dispatcher, group_id, user_id, service.list_memories, user_id, group_id, value)
    if action == "add":
        memory_type, sep, content = value.partition("|")
        if not sep:
            memory_type, content = "note", value
        return await _run(dispatcher, group_id, user_id, service.add_memory, user_id, group_id, memory_type.strip(), content.strip())
    if action == "update":
        memory_id, sep, content = value.partition("|")
        return await _run(dispatcher, group_id, user_id, service.update_memory, user_id, group_id, memory_id.strip(), content.strip() if sep else "")
    if action in {"lock", "archive"}:
        return await _run(dispatcher, group_id, user_id, service.set_memory_state, user_id, group_id, value, action)
    await dispatcher._reply(group_id, user_id, "用法：/memory list | search <关键词> | add <类型> | <内容> | update <编号> | <内容> | lock <编号> | archive <编号>")


async def cmd_world(dispatcher, group_id, user_id, args, role, sender_card, message):
    service = _service(dispatcher)
    parts = args.strip().split(maxsplit=1)
    action = parts[0].lower() if parts else "list"
    value = parts[1].strip() if len(parts) > 1 else ""
    if action == "list":
        return await _run(dispatcher, group_id, user_id, service.list_worldbooks, user_id, group_id)
    if action == "show":
        return await _run(dispatcher, group_id, user_id, service.show_worldbook, user_id, group_id, value)
    if action == "delete":
        target, separator, confirmation = value.rpartition("|")
        if not separator:
            target, confirmation = value, ""
        return await _run(dispatcher, group_id, user_id, service.delete_worldbook, user_id, group_id, target.strip(), confirmation.strip())
    if action == "add":
        fields = [item.strip() for item in value.split("|", 2)]
        if len(fields) != 3:
            await dispatcher._reply(group_id, user_id, "用法：/world add <世界书> | <关键词> | <内容>")
            return
        return await _run(dispatcher, group_id, user_id, service.add_world_entry, user_id, group_id, *fields)
    if action == "use":
        return await _run(dispatcher, group_id, user_id, service.bind_worldbook, user_id, group_id, value)
    await dispatcher._reply(group_id, user_id, "用法：/world list | show <世界书> | add <世界书> | <关键词> | <内容> | use <世界书> | delete <世界书> | 确认")


async def cmd_mode(dispatcher, group_id, user_id, args, role, sender_card, message):
    service = _service(dispatcher)
    value = args.strip()
    if not value or value.lower() == "status":
        return await _run(dispatcher, group_id, user_id, service.status, user_id, group_id)
    return await _run(dispatcher, group_id, user_id, service.set_mode, user_id, group_id, value)


async def cmd_scene(dispatcher, group_id, user_id, args, role, sender_card, message):
    service = _service(dispatcher)
    parts = args.strip().split(maxsplit=1)
    action = parts[0].lower() if parts else "status"
    value = parts[1].strip() if len(parts) > 1 else ""
    if action == "status":
        return await _run(dispatcher, group_id, user_id, service.scene_status, user_id, group_id)
    if action == "set":
        return await _run(dispatcher, group_id, user_id, service.scene_set, user_id, group_id, value)
    if action == "change":
        return await _run(dispatcher, group_id, user_id, service.scene_set, user_id, group_id, value, change_scene=True)
    if action == "beat":
        return await _run(dispatcher, group_id, user_id, service.scene_beat, user_id, group_id, value)
    if action == "memory":
        tier, _, payload = value.partition(" ")
        return await _run(dispatcher, group_id, user_id, service.scene_memory, user_id, group_id, tier, payload)
    await dispatcher._reply(group_id, user_id, "用法：/scene status | set 字段=值 | change 字段=值 | beat <内容> | memory stable/volatile key=value")


async def cmd_bond(dispatcher, group_id, user_id, args, role, sender_card, message):
    service = _service(dispatcher)
    return await _run(dispatcher, group_id, user_id, service.bond_timeline, user_id, group_id)
