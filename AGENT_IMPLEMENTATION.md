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
- `bot/agent/tools/`：现有 UApiS/AI 工具和 NapCat 白名单网关。
- `bot/agent/tools/native.py`：目标、提醒和后台任务查询等 Agent 原生工具。
- `bot/agent/workers.py`：后台任务记录。
- `bot/agent/worker_service.py`：可关闭的 Agent Worker 生命周期。
- `bot/commands/agent.py`：人工查看和切换入口。

## 开启与回滚

最高主人发送 `/agent on` 才会开启 Agent 主路由；发送 `/agent off` 可立即恢复旧 AI 主路由。群主可在当前群发送 `/agent 主动 on` 或 `/agent 主动 off` 控制群域主动候选。

生产部署建议先保持默认观察模式至少 24 小时，检查 `data/agent/events/`、服务错误日志和磁盘增长，再只对最高主人私聊开启主路由。

## 安全边界

- 群主不拥有跨群或最高主人私人工作区。
- 普通成员默认不进入主动 Agent 候选。
- `get_cookies`、凭证、CSRF、RKey、原始 packet 和测试动作永久拒绝。
- Agent 配置通过现有脱敏保存函数写回，不落盘运行时 API 密钥。
- 所有 Agent JSON 状态使用原子替换并限制记录数量。

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
- 群聊不会继承最高主人私域自治；必须由当前群主在群内发送 `/agent 主动 on` 单独授权。

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
- `/agent 自治 on|off`：开关最高主人私域自治。
- `/agent 主动 on|off`：开关当前群的 Agent 主路由和主动候选。

规划器可以在当前已认证作用域内调用以下原生工具：

- `agent_create_goal`、`agent_update_goal`、`agent_list_goals`
- `agent_create_reminder`、`agent_list_reminders`
- `agent_list_tasks`

这些工具始终绑定当前私聊或群聊作用域，模型不能通过参数切换到其他群或最高主人私域。
