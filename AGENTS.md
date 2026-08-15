# AGENTS.md

本文件适用于仓库根目录及其全部子目录。它用于指导开发者和 AI 编码代理在本项目中进行分析、修改、测试、审查和部署。

如子目录中存在更具体的 `AGENTS.md`，以距离目标文件最近的规则为准。

## 项目简介

小汐是一个运行在 NapCat 上的 QQ 机器人，使用 OneBot v11 反向 WebSocket 协议。项目主要能力包括：

- 群聊和私聊 AI 对话、上下文记忆、联网搜索和图片理解。
- QQ 群管理、权限分级、内容审核和高风险操作确认。
- Bilibili、TouchGal、UApiS、Mukyu、NapCat 等外部服务集成。
- 定时推送、表情包收集、角色扮演和持久化 Agent 工作区。
- systemd 托管、运行日志、健康检查、备份和回滚。

生产入口是 `main.py`，生产目录是 `/opt/qqbot`，正式分支是 `main`。

## 资料入口

开始工作前，根据任务阅读相关文档：

- `README.md`：功能、命令、配置和用户使用说明。
- `docs/architecture.md`：模块边界、兼容入口和行为冻结规则。
- `docs/deployment.md`：测试、分阶段部署和回滚流程。
- `docs/operations.md`：服务、日志、备份和生产运维。
- `docs/roleplay.md`：角色扮演模块的数据和行为约束。
- `docs/workspace.md`：本地目录、临时文件、Git 与三方同步规范。
- `CLAUDE.md`：较完整的历史维护说明，仅作为补充参考。

代码和当前测试是最终事实来源。文档与实现不一致时，应先确认现有行为，再同时修正文档和代码。

## 目录边界

```text
main.py                    稳定入口，只负责日志和启动流程
app/                       配置、日志和进程生命周期
bot/ai/                    AI Provider、提示词、记忆、搜索和回复解析
bot/agent/                 Agent 计划、工具、存储、验证和后台任务
bot/roleplay/              角色扮演（角色卡、会话、世界书、SQLite 持久化）
bot/commands/              命令注册及按领域拆分的命令实现
bot/events/                消息、通知、请求和路由
bot/transport/             OneBot WebSocket、动作和消息输出
bot/integrations/          Bilibili、TouchGal、UApiS、Mukyu、NapCat 等集成
bot/services/              调度、健康检查、确认和延迟回复
bot/security/              URL 检查和安全审计
bot/storage/               运行路径和原子持久化
deploy/                    systemd、日志、备份和部署脚本
scripts/                   合约同步、迁移和维护脚本
tests/                     unit、integration、regression 测试
docs/                      架构、部署和运维文档
```

`bot.client`、`bot.bilibili`、`bot.scheduler`、`bot.touchgal`、`bot.uapi` 等旧模块是兼容入口。新代码应导入聚焦后的 canonical 模块，不得随意删除兼容入口。

## 工作区边界

- 一个工作区只保留一个小汐正式 checkout，目录名统一为 `xiaoxi-bot`。不得同时维护 `audit-live-*`、`work*`、`source` 或其他内容相同的临时 clone。
- 外部角色扮演、NapCat 或协议参考项目统一放在正式仓库同级的 `references/` 下。它们是独立仓库，不得纳入小汐 Git、测试或部署。
- 参考仓库内可能存在未提交修改。除非任务明确要求修改参考项目，不得清理、重置或覆盖其 Git 状态。
- 临时审计、部署验证和解包目录使用系统临时目录，并在任务完成后删除；不要把 bundle、tar、patch、日志或虚拟环境堆在工作区父目录。
- 生产配置、密钥和运行数据不通过 Git 同步。工作区整理不得触碰 `/var/lib/qqbot/config.json`、`/etc/qqbot.env` 或 `/opt/qqbot/data/`。
- 具体布局与三方核对命令见 `docs/workspace.md`。

## 不可破坏的约束

1. 不得把 API Key、OneBot Token、Cookie、QQ 账号数据或服务器凭据写入 Git。
2. 密钥只从环境变量或 `/etc/qqbot.env` 读取；不要写入 `config.json`、测试快照或日志。
3. 不得覆盖或清空生产运行数据，包括 `/var/lib/qqbot/config.json`、`/opt/qqbot/data/` 和用户记忆。
4. AI 输出不是权限证明。权限、目标身份和危险操作必须由确定性的 Python 代码校验。
5. 群管理、文件删除、跨群操作等高风险能力必须保留权限检查和确认机制。
6. 不得在 journald 或普通诊断日志中记录聊天正文、Token、Cookie、完整请求头或敏感账号信息。
7. 不得重写已经发布的 `main` 历史，不使用强推，不把生产服务器当作开发源仓库。
8. 结构性重构不得无意改变命令行为、提示词、权限、消息段、配置格式或 JSON 数据格式。

## Python 代码规范

- 支持 Python 3.10 及以上版本；CI 使用 Python 3.12。
- 使用 4 个空格缩进、UTF-8、LF 行尾和清晰的英文标识符。
- 面向用户的消息可使用中文；代码注释只解释不明显的原因、边界或协议约束。
- 新增或修改公共函数时补充准确的类型标注；避免无意义的 `Any` 和宽泛异常捕获。
- 优先使用现有 dataclass、枚举、配置模型和辅助函数，不重复实现相同抽象。
- 函数保持单一职责。大型命令或集成逻辑应放入对应领域模块，不继续膨胀兼容 facade。
- 使用 `pathlib.Path`、结构化 JSON API 和现有原子写入工具，不手工拼接结构化数据。
- 除非确有必要且已说明原因，不新增第三方依赖。
- 不做与当前任务无关的大规模格式化、重命名或重构。

### 异步和网络代码

- 事件循环中不得执行阻塞式网络或磁盘操作；必要时使用现有异步客户端或线程卸载模式。
- 所有外部请求都应有超时、有限重试和明确的降级行为，禁止无限重试。
- 后台任务应能在退出时取消并回收，不得遗留失控的 Task、TimerHandle 或临时文件。
- 调用 OneBot 前检查连接状态；发送失败不能提前推进持久化的“已发送”状态。
- 对外部 API 响应做类型和边界校验，不信任缺失字段、错误码或异常 Content-Type。
- 调度时间默认按 `Asia/Shanghai` 处理，避免使用隐式服务器本地时区。

### 配置和持久化

- 配置读取、迁移和环境变量覆盖集中在 `app/config.py`。
- 运行路径统一通过 `bot/storage/runtime_paths.py` 等现有入口解析。
- JSON 状态更新必须保持原子性；共享数据需要沿用现有锁和更新帮助函数。
- 新增配置项时必须提供安全默认值、迁移兼容和对应测试。
- 临时文件放入项目约定的运行临时目录，使用后及时删除。

### 权限和 Agent 工具

- 命令权限在注册表和 `bot/permission.py` 中统一表达，不在命令正文中复制一套权限体系。
- 操作者必须严格高于受管理目标；受保护账号和机器人主人边界不得绕过。
- AI 工具只暴露完成任务所需的最小能力。管理 API 不得直接交给模型自由调用。
- 高风险 Agent 计划必须经过 verifier、冻结工具集合和确认流程后执行。
- 新工具需要覆盖：参数校验、权限拒绝、超时、API 失败和成功路径测试。

### 日志和错误处理

- 日志应描述事件、作用域和结果，不记录完整消息正文或密钥。
- 对 Token、Cookie、Authorization、URL 查询凭据和 QQ 敏感字段进行脱敏。
- 可预期的外部失败应降级并给出可诊断日志；编程错误不得被空 `except` 静默吞掉。
- 健康检查和 watchdog 不应制造重启风暴；失败计数、冷却和恢复必须有界。

## 中文和 Windows 注意事项

- 仓库中文文件使用 UTF-8。PowerShell 读取时显式使用 `Get-Content -Encoding utf8`。
- 不要因为终端显示乱码就批量重写中文文件；先检查文件真实编码和 Git diff。
- 避免把包含中文的大段内容直接嵌入 SSH 命令。优先在本地修改、测试，再上传或通过 Git 部署。
- 提交前检查是否产生意外的 CRLF 转换或整文件编码变化。

## 测试要求

修改代码后至少运行与改动直接相关的测试。合并或部署前运行完整验证：

```bash
git diff --check
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m ruff check . --no-cache --select E9,F524,F63,F7,F82,F811,F841,B023,ASYNC221,ASYNC230,S110,S112,PLW0602,PLW0211,G201
python -m bandit -q -r app bot deploy -x tests --severity-level high --confidence-level high
python scripts/sync_api_contracts.py --check
python -m compileall -q -f .
python -m unittest discover -s tests -t . -v
```

测试策略：

- 修复缺陷时先补能复现问题的回归测试。
- 修改权限、配置、持久化、消息发送或调度时，同时覆盖拒绝和失败路径。
- 修改兼容入口时运行导入兼容和架构测试。
- 修改 systemd、日志、备份或部署脚本时运行 `tests/unit/test_deployment.py`。
- 测试不得访问真实 QQ、生产数据或付费 API；使用 mock、临时目录和固定输入。
- 不通过删除、放宽或跳过测试来让 CI 变绿。

GitHub 的 `test` 状态检查是 `main` 的必需检查。完整验证未通过时不得部署。

## Git 工作流

- 只在标准 `xiaoxi-bot/` checkout 中开发；父目录只保留该仓库和 `references/`。
- 开始任务前执行 `git fetch --prune --tags`，确认工作区干净并从最新 `main` 创建分支。
- 分支名使用 `feat/<topic>`、`fix/<topic>`、`refactor/<topic>`、`docs/<topic>` 或 `chore/<topic>`。
- 一个提交只解决一个明确问题；提交信息说明行为变化，例如 `fix: keep scheduler retries bounded`。
- 不提交 `data/`、日志、临时下载、虚拟环境、备份包、密钥文件或本地探针输出。
- 通过 PR 合并到 `main`，使用 squash merge。GitHub 会在合并后自动删除远程分支。
- `main` 保持线性历史；禁止 force-push 和删除保护分支。
- 回滚点使用仓库外的 `.bundle` 或 tar 备份，不在生产仓库长期堆积 `backup/*` 分支；验证完成后只保留必要的最新归档。
- 提交或部署后再次确认 `git status` 干净且本地、GitHub、服务器指向同一提交。

## 部署规则

生产服务器：

```text
SSH:        使用运维环境提供的主机、端口和密钥，不写入仓库
Repository: /opt/qqbot
Environment: /etc/qqbot.env
Services:   qqbot.service, napcat.service
Backups:    /root/qqbot-backups/
```

自动备份至少保留最新 5 份，并只删除超过 30 天的普通归档。部署前可额外创建一个已验证回滚点，但验证完成后不得长期堆积临时 bundle、隔离目录或 Git stash。

部署必须遵循以下顺序：

1. 在本地分支完成修改和完整测试。
2. 通过 PR squash 合并，确认 GitHub CI 成功。
3. 部署前创建一个可验证的 tar 或 Git bundle 回滚点。
4. 先在隔离目录使用生产 Python 环境验证上传内容。
5. 再更新 `/opt/qqbot`，确保服务器 `main` 与 `origin/main` 一致。
6. 只重启受影响的服务，确认 `qqbot.service` 和 `napcat.service` 为 `active`。
7. 检查 OneBot 连接、近期异常、服务重启次数、仓库状态和日志脱敏。
8. 验证失败时立即停止继续发布，并按 `docs/deployment.md` 回滚。

不要直接在生产服务器编辑并提交代码。紧急修改也应下载到本地、形成提交、通过测试后再部署。

## 完成标准

任务只有在以下条件满足后才算完成：

- 行为符合请求，且没有越过现有模块和权限边界。
- 相关测试和完整 CI 检查通过，或明确记录无法运行的原因。
- 文档、配置样例和 API 合约与实现同步。
- Git diff 中没有密钥、聊天正文、生成文件或无关改动。
- 工作区干净，提交可追溯，生产部署有回滚点。
- 本机、GitHub `main` 与生产服务器 `/opt/qqbot` 指向完全相同的提交。
- 最终反馈列出修改内容、验证结果、部署状态和仍存在的风险。
