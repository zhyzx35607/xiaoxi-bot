# 主人私聊角色扮演系统

## 功能

角色扮演系统只在配置的最高主人私聊中开放，提供：

- SillyTavern Character Card JSON v2 和 PNG 元数据导入，包括角色系统指令和历史后置指令；
- Persona 创建、选择和删除；
- 多角色、多聊天、新建、切换、重命名、归档和 JSON 导出；
- 结构化记忆的添加、检索、更新、锁定和归档；
- 世界书的关键词触发和聊天绑定；
- 可选的 LightRAG 混合检索；
- 运行时私有策略文件；
- 请求和响应哈希审计。

## 命令

- `/char list|import|show|export|delete <角色> | 确认`
- `/persona list|create|use|delete`
- `/chat current|list|new|use|rename|export|delete`
- `/memory list|search|add|update|lock|archive`
- `/world list|show|add|use|delete <世界书> | 确认`
- `/mode normal|story|status`
- `/scene status|set|change|beat|memory`
- `/bond`

所有管理命令同时经过现有 `bot_owner_only` 权限检查和服务内部的主人私聊校验。
记忆的更新、锁定和归档只作用于当前聊天。角色和世界书删除均要求显式发送 `确认`。

## 运行时目录

以下目录在 `.gitignore` 中，不进入公开仓库：

- `data/roleplay.sqlite3*`
- `data/roleplay_imports/`
- `data/roleplay_exports/`
- `data/roleplay_private/`

角色卡必须先放入 `data/roleplay_imports/`，然后使用相对文件名导入。

## 私有策略

可在服务器创建：

`data/roleplay_private/policies.json`

格式：

```json
{
  "owner_story": "仅在运行服务器保存的自定义叙事策略"
}
```

仓库只保存策略加载机制和通用默认模式，具体私有策略由运行环境管理。

`/mode story` 会额外启用连续叙事质量规则：保持角色、视角、关系、位置和动作连续，按回合推进剧情节拍，并使用连贯自然段。角色扮演激活期间不会调用外部工具，内容只进入专用角色扮演数据库，不写入普通私聊记忆。

生成参数可在 `roleplay` 配置段调整：

- `response_max_tokens`：单回合生成预算，范围 300 到 2400，默认 1200；
- `response_temperature`：生成温度，范围 0.1 到 1.5，默认 0.82。
- `story_unbounded_tokens`：story 模式默认开启；开启时请求不发送 `max_tokens` 字段，由模型服务端决定最大生成长度。normal 模式仍使用 `response_max_tokens`。

## LightRAG

在配置中的 `roleplay.lightrag` 设置：

- `enabled`
- `base_url`
- `mode`
- `timeout_seconds`
- `max_context_chars`

QQ Bot 调用 `/health` 和 `/query`。LightRAG 超时或不可用时自动降级到 SQLite 摘要、结构化记忆和最近消息，不阻塞主聊天。

## 数据边界

角色卡、Persona、世界书、记忆和检索结果都只作为上下文数据，不授予工具权限，也不覆盖系统策略、Agent 工具预算和外部操作确认流程。


## 场景、节拍和关系时间线

借鉴 MuseAI 的状态模型，当前聊天额外保存：

- 场景标题、现状、时间、地点、活动角色和剧情进度；
- stable 场景记忆：跨场景保留的世界规则和长期状态；
- volatile 场景记忆：切换场景时清空的线索和临时状态；
- 每次角色回复对应的剧情节拍；
- relationship_event 和 relationship_state 类型记忆组成的关系时间线。

切换场景会生成新的 scene_id，保留 stable 记忆并清空 volatile 记忆。
