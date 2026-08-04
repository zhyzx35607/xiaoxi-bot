# Agent 实施与部署说明

## 当前阶段

Agent 已作为独立模块接入现有 QQ Bot，默认配置为：

- `agent.observation_only = true`
- `agent.primary_router = false`
- `agent.owner_autonomy_enabled = false`
- `agent.worker_enabled = true`

因此部署后 Worker 会负责确定性提醒、后台任务和目标复盘调度，但不会自动开启最高主人自治，也不会替换旧 AI 主路由。

## 已实现模块

- `bot/agent/models.py`：身份、作用域、事件、决策和记忆候选契约。
- `bot/agent/identity.py`：最高主人、群主、管理员、普通成员身份解析。
- `bot/agent/policy.py`：静默时间、频率预算、群域边界和敏感工具拒绝。
- `bot/agent/memory.py`：私域/群域隔离的候选、确认记忆。
- `bot/agent/proactive.py`：每日额度、主题冷却、拒绝后静默。
- `bot/agent/planner.py`：结构化 DeepSeek 规划。
- `bot/agent/plans.py`：持久化多步计划、步骤状态、成功标准与执行证据。
- `bot/agent/timeline.py`：按作用域记录消息、计划、工具、确认、任务与主动行动时间线。
- `bot/agent/insights.py`：带置信度、证据和来源的长期反思/洞察。
- `bot/agent/skills.py`：当前私域或群域可复用的技能/SOP。
- `bot/agent/profiles.py`：最高主人私域或单个 QQ 群的专属人设、规则和主动主题。
- `bot/agent/tools/`：现有 UApiS/AI 工具和 NapCat 白名单网关。
- `bot/agent/tools/native.py`：目标、提醒和后台任务查询等 Agent 原生工具。
- `bot/agent/tools/napcat.py`：由 API 注册表驱动的只读 NapCat 工具，并强制绑定当前群域。
- `bot/agent/workers.py`：后台任务记录。
- `bot/agent/worker_service.py`：可关闭的 Agent Worker 生命周期。
- `bot/commands/agent.py`：人工查看和切换入口。

## 开启与回滚

最高主人发送 `/agent on` 才会开启 Agent 主路由；发送 `/agent off` 可立即恢复旧 AI 主路由。群主可在当前群发送 `/agent 主动 on` 或 `/agent 主动 off` 控制群域主动候选。

生产部署建议先保持默认观察模式至少 24 小时，检查 `data/agent/events/`、服务错误日志和磁盘增长，再只对最高主人私聊开启主路由。

## 安全边界

- 群主不拥有跨群或最高主人私人工作区。
- 群域 Agent 可以在全局最高主人自治保持关闭时独立启用；每个群必须单独授权。
- 普通成员默认不进入主动 Agent 候选。
- `get_cookies`、凭证、CSRF、RKey、原始 packet 和测试动作永久拒绝。
- Agent 配置通过现有脱敏保存函数写回，不落盘运行时 API 密钥。
- 所有 Agent JSON 状态使用原子替换并限制记录数量。
- 群主方案要求确认时，确认前不会执行任何工具；确认后只执行被冻结的工具集合。
- 密码、Token、Cookie、API Key、CSRF、RKey 等秘密不会进入 Agent 记忆。
- 群域只读工具强制使用当前群号；全局读取能力仅允许最高主人私聊使用。

## 验证与部署命令

本地：

```powershell
python -m unittest tests.unit.test_agent -v
python -m unittest discover -s tests -v
```

服务器部署前备份：

```bash
cp -a /opt/qqbot /opt/qqbot.backup-agent-20260804
```

部署后：

```bash
python -m unittest discover -s tests -v
systemctl restart qqbot.service
systemctl is-active qqbot.service
journalctl -u qqbot.service -n 100 --no-pager
```

回滚：

```bash
systemctl stop qqbot.service
rsync -a --delete /opt/qqbot.backup-agent-20260804/ /opt/qqbot/
systemctl start qqbot.service
```

## 最高主人自治模式

最高主人可在私聊发送 `/agent 自治 on`。开启后：

- 单次消息最多进行 6 轮“规划 → 安全工具 → 结果回灌 → 再规划”。
- 单次最多调用 12 个白名单工具，每个工具默认 15 秒超时。
- 复杂工作可转为后台任务，任务执行后必须按成功标准验收；失败最多重试 3 次。
- 后台任务完成或最终失败时会主动私聊汇报。
- 存在长期目标时，默认每 2 小时最多主动复盘一次，并继续受每日 6 次、23:00-09:00 静默和主题冷却限制。
- 群聊不会继承最高主人私域自治；必须由当前群主在群内发送 `/agent 主动 on` 单独授权。群域默认每 3 小时最多复盘一次。
- 群主或最高主人明确说“别主动了”“安静点”等会让当前作用域静默 12 小时，可用 `/agent 恢复主动` 提前恢复。

常用命令：

- `/agent`：查看状态、目标、提醒、后台任务和待确认记忆数量。
- `/agent 目标 add 内容`：创建长期目标。
- `/agent 目标 done ID`：完成目标。
- `/agent 提醒 add 30分钟 内容`：创建确定性提醒。
- `/agent 提醒 cancel ID`：取消提醒。
- `/agent 记忆`：查看待确认记忆。
- `/agent 记忆 confirm 序号`：确认敏感记忆。
- `/agent 任务 list`：查看当前作用域后台任务。
- `/agent 任务 cancel ID`：取消当前作用域尚未结束的后台任务。
- `/agent 计划`：查看当前作用域计划列表。
- `/agent 计划 ID`：查看逐步状态、成功标准和执行证据。
- `/agent 计划 cancel ID`：取消未结束计划。
- `/agent 技能 add 名称 | 触发词1,触发词2 | SOP 指令`：建立当前作用域技能。
- `/agent 技能 on|off ID`：启用或停用技能。
- `/agent 画像 人设 内容`：设置当前作用域专属人设。
- `/agent 画像 规则 内容`：设置当前作用域习惯与规则。
- `/agent 画像 主题 主题1,主题2`：设置可主动关注主题。
- `/agent 时间线`：查看最近 Agent 行动记录。
- `/agent 洞察`：查看已沉淀的反思与证据。
- `/agent 静默 12小时`、`/agent 恢复主动`：控制当前作用域主动消息。
- `/agent 自治 on|off`：开关最高主人私域自治。
- `/agent 主动 on|off`：开关当前群的 Agent 主路由和主动候选。

规划器可以在当前已认证作用域内调用以下原生工具：

- `agent_create_goal`、`agent_update_goal`、`agent_list_goals`
- `agent_create_reminder`、`agent_list_reminders`
- `agent_list_tasks`
- `agent_create_plan`、`agent_update_plan_step`、`agent_list_plans`
- `agent_add_insight`、`agent_list_insights`、`agent_list_timeline`
- `agent_create_skill`、`agent_list_skills`

这些工具始终绑定当前私聊或群聊作用域，模型不能通过参数切换到其他群或最高主人私域。
