# 小汐 — QQ 群聊机器人

一个跑在 [NapCat](https://github.com/NapNeko/NapCatQQ) 上的 QQ 机器人，OneBot v11 协议。

小汐的人设是一个 20 岁的中文系大二女生，性格温柔好说话，爱刷手机爱追番打游戏。她能在群里闲聊接话、帮忙管群、收表情包、认图片里的字，私聊也能聊。默认走 SigmaI（DeepSeek-V4-Flash），DeepSeek 官方 API 做后备。

## 她能干什么

**聊天方面：**
- 群里 @她或者叫"小汐"，她就会回你
- 有时候她会自己判断语境插话——比如有人在问问题、聊到她懂的话题，她可能就冒出来了
- 遇到她不确定的事实性问题，会自动去网上搜一下再回答（用的 Bing，免费）
- 能看懂图片：你发张图她可以说说是什么，也能 OCR 提取上面的文字（优先用 Vision API（如 qwen-vl-plus）识图，失败则用 QQ 自带摘要）
- 能看合并转发的内容
- 能记住最近聊了什么（短期 20 条 + 长期压缩摘要）

**管群方面：**
- 有人进群自动欢迎，欢迎语可以自定义
- 违禁词检测，自动撤回加警告
- 黑名单系统，按群分开，到期自动解
- 链接安全检查，危险链接自动撤回去禁言
- R18 内容识别，三次警告自动拉黑 48 小时

**互动娱乐：**
- 今日运势（AI 生成的，每人每天一次）
- 发言排行
- 戳一戳自动回戳
- 点赞秒回：有人给你点赞，一秒回满（SVIP 回 20 个，普通号回 10 个）
- 复读机：群友好几个人发同一句话，她概率跟风
- 表情包：自动收集并用 AI 分析情绪标签，AI 聊天时能自主选择贴合语境的表情包发送
- 随机图片：通过 Mukyu 图片服务按标签、横竖图、清晰度、AI 类型和作品类型选图；R18/混合范围只对最高主人和群主人开放
- 点歌：说"来首 xxx"就能搜
- 娱乐查询：真实天气、各平台热榜（AI 概括 + 可点链接）、一言、答案之书、每日新闻图、必应壁纸、Epic 免费游戏（数据来自 uapis.cn，有每日积分预算控制）
- B站功能：群里发 B站视频链接/BV号/b23 短链（包括 QQ 分享卡片），自动解析出标题、封面、播放量等信息，并尽量把视频本体发出来（免登录官方接口，超限自动降级为只发信息）
- UP 主推送：每个群可以盯几个 B站 UP 主，新投稿约 1 分钟内推到群里（Bot 是管理会顺便 @全体）
- Galgame 资源查询：通过 TouchGal 官方 API 搜索作品、识别平台并返回官方详情/资源页，不发送网盘直链；群内可按群开关自动识别。
- 定时内容：每天在 08-11、12-15、16-19、20-23 四个时段各随机发送一组 ACG 图片，在 10-13、19-22 两个时段各随机发送一份有来源的微博摘要；每组 ACG 图片固定 20 张。

**AI 工具调用：**
- AI 聊天时能自己调用只读工具查群信息、查天气热榜等（最多连调 2 轮）
- 在被 @ 或追问的场景下，还能给消息贴表情、给群友点赞（每群每天限 30 次，插话场景不会用）
- 管理类操作永远不让 AI 碰，权限判断全在代码里

**权限体系（五档）：**
- 总主人 > 群主人 > 群主 > 管理员 > 普通成员，机器人账号本身等同于总主人
- 群主人只能由总主人（或机器人账号自己）添加/移除
- Bot 是群管理时，管理员/群主/主人都能用禁言、踢人、公告、@全体 等管理命令

**私聊：**
- 私聊 AI 默认**关闭**，主人用 `/私聊AI on` 开启（所有好友可聊），或 `/私聊AI allow QQ号` 只开放给指定的人
- 非好友私聊完全静默，不会收到任何回复
- Bot 主人可以在私聊里用管理命令、看日志、处理加群申请
- 登录机器人账号自己发的消息也能触发固定指令（在群里发命令、或在主人的私聊窗口发命令都行）

## 怎么跑起来

先装 NapCat，让它开个 WebSocket 服务端监听 `ws://127.0.0.1:3001`。

然后：

```bash
git clone https://github.com/zhyzx35607/xiaoxi-bot.git
cd xiaoxi-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 工作区约定

本机只保留一个正式的 `xiaoxi-bot/` checkout。角色扮演、NapCat 和协议参考项目放在同级 `references/` 中，各自保留独立 Git 状态，不复制进主仓库：

```text
qqbot/
├── xiaoxi-bot/
└── references/
```

审计快照、部署包和临时 clone 放到系统临时目录，用完即删。完整约定和本机、GitHub、服务器三方核对命令见 `docs/workspace.md`。

配置就靠环境变量，密钥不要写进 config.json：

```bash
export SIGMAI_API_KEY="sk-xxxxxxxxxxxxxxxx"      # SigmaI 的 key，主聊天模型
export DEEPSEEK_API_KEY="sk-xxxxxxxxxxxxxxxx"    # DeepSeek 的 key，后备（没有可不留空）
export QQBOT_WS_URL="ws://127.0.0.1:3001"        # NapCat 的 WS 地址
export QQBOT_TOKEN=""                             # OneBot access token；注意：如果 NapCat 的 WS 服务端配了 token 这里就必须填一致，否则会连上就被踢、每秒重连
export UAPI_API_KEY="uapi-xxxxxxxxxxxxxxxx"        # uapis.cn 的 key，娱乐查询/B站推送备用通道用（没有则这些功能静默停用）
export MUKYU_API_KEY=""                             # Mukyu 图片服务可选 key；匿名接口可用时可以留空
export TOUCHGAL_API_TOKEN="tg-xxxxxxxxxxxxxxxx"     # TouchGal API Token；没有时 /gal 可用 status 查看状态
```

如果要用图片识别和表情包分析功能，再配 Vision API（推荐开启，免费额度足够用）：

```bash
export VISION_API_KEY="sk-xxxxxxxxxxxxxxxx"
export VISION_API_BASE_URL="https://your-api.com/v1"
export VISION_API_MODEL="qwen-vl-plus"
```

启动（手动跑之前记得先加载环境变量，否则 WS 会因缺 token 被 NapCat 秒踢）：

```bash
set -a; source /etc/qqbot.env; set +a   # 如果 env 文件存在的话
python main.py
```

用 systemd 托管更稳：

```bash
sudo cp deploy/qqbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qqbot.service
```

config.json 里可以按群开关各种功能，调整 AI 插话的积极性、频率限制等等。用到的时候看看文件里的注释就行。

ACG 图片从 `i.mukyu.ru` 以 `r18=0` 选取并进入持久化图片池。定时任务收齐 20 张不重复图片后，以一组合并转发发送；已发送 URL 在 7 天后可重新入选。切换图片服务时会先清除旧 UApiS 图片池。

## 命令一览

### Agent 工作区

群主或最高主人可用 `/agent` 管理当前作用域的目标、持久化多步计划、提醒、后台任务、记忆、技能/SOP、群画像、洞察和行动时间线。最高主人私聊可用 `/agent 自治 on` 开启完整自治；群主在当前群使用 `/agent 主动 on` 可独立开启本群 Agent，不会继承或读取其他群及最高主人私域。

群域 Agent 的写入和高风险方案受身份与确认机制保护：需要确认的方案在 `/确认 CODE` 前不会执行工具，确认后也只能执行已冻结的工具集合。明确说“别主动了”或使用 `/agent 静默 12小时` 会暂停当前作用域主动消息。

群里用 `/` 前缀，也有些话不带前缀也能触发。

**所有人都能用：**

`/help` — 看有哪些命令（按你的身份分级显示）
`/help 命令名` — 看某个命令的详细用法
`/fortune` — 今日运势
`/like @xxx` — 点赞
`/rank` — 发言排行
`/天气 城市`（或 `/weather`）— 真实天气
`/热榜 [平台]`（或 `/热搜`）— 各平台热榜（微博/知乎/B站/抖音/百度/头条/IT之家/GitHub…）
`/一言` — 随机一言
`/答案之书 [问题]` — 答案之书
`/每日新闻` — 每日新闻图
`/必应壁纸` — 每日必应壁纸
`/gal 作品名 [平台]` — TouchGal Galgame 资源/详情页查询（支持 安卓、KRKR、Windows、PE）
`/gal status` — 查看 TouchGal 开关与 Token 状态
`/epic免费` — Epic 免费游戏
`/translate 文本` — 翻译
`/calc 1+2*3` — 计算器
`/ocr` — 识别图片文字（回复那张图发）
`/随机图 [标签与范围]`（或 `/pixiv图`）— 随机选一张图片；支持 `标签=初音ミク,ボーカロイド`、`且/或`、`横图/竖图/方图`、`高清/超清`、`非AI/AI`、`插画/漫画/动图`
`/info @xxx` 或 `/info QQ号` — 查成员资料
`/history [条数]` — 最近消息
`/精华列表` — 群精华
`/群荣誉` — 龙王/群火之类的荣誉
`/群文件 [关键词]` — 搜群文件
`/文件链接 file_id busid` — 取文件下载链接
`/禁言列表` — 看谁在被禁言
`/已读` — 标记消息已读
`/转发` — 转发消息（回复目标消息发）
`/点赞信息` — 看机器人的点赞数据

**管理用的（要群管 + Bot 也得是管理）：**

`/kick @xxx` — 踢人
`/ban @xxx [分钟]` — 禁言
`/unban @xxx` — 解禁
`/allban on/off` — 全员禁言
`/welcome` — 设置欢迎语
`/badword` — 违禁词管理
`/admin add/del @xxx` — 上/下管理
`/精华` — 设精华（回复那条消息）
`/删精华` — 取消精华
`/公告` — 群公告
`/setgroupavatar` — 换群头像（回复图片）
`/安全 status/log` — 安全功能状态和日志
`/全体 内容` — @全体成员
`/acg图 on/off` — 本群每日 ACG 图推送开关
`/热榜推送 on/off` — 本群每日热榜推送开关
`/b站解析 on/off` — 本群 B站自动解析开关
`/gal资源 on/off` — 本群 Galgame 资源自动回复开关

**群主人（每群的主人，由总主人设置）：**

`/enable` `/disable` — 群聊中开关本群；最高主人私聊时需明确写群号或 `all`
`/list` — 群数据概览
`/clearai` — 清本群数据（确认后先备份再清理）；最高主人私聊时需明确写群号或 `all`
`/b站推送 add/del/list` — 盯 UP 主新投稿（mid 是 UP 主空间网址 space.bilibili.com/ 后面的数字，直接贴空间链接也行；详细用法发 `/help b站推送`）
`/积分` — 看 uapis 积分额度

最高主人和群主人可在 `/随机图` 后追加 `R18` 或 `混合`；其他身份始终固定为全年龄范围，并且响应元数据会再次校验 `x_restrict=0`。

**群主才能用的（QQ 群主身份）：**

`/title @xxx 头衔` — 设专属头衔（Bot 得是群主）

**Bot 主人的私聊命令（在私聊窗口发给 Bot）：**

`/status` — 看运行状态、内存、在线时间
`/AI状态` — 看 SigmaI / DeepSeek 供应商状态
`/私聊AI on/off/allow QQ/deny QQ` — 私聊 AI 总开关与开放名单
`/AI聊天 on/off` — 开关本群的 AI 聊天（私聊里用 `/AI聊天 群号 on/off`）
`/打卡状态` `/打卡测试 群号` — 群打卡
`/list` — 所有群的概览
`/log [N]` — 看最近 N 条日志
`/bl list/add/remove` — 黑名单管理
`/group enable/disable/list 群号` — 开关群
`/memory 群号` — 看群的 AI 记忆
`/memory clear 群号` — 清掉
`/sticker 群号` — 看收了多少表情包
`/sysmsg` — 看加群申请
`/approve flag尾号` — 同意加群
`/reject flag尾号 原因` — 拒绝
`/health` — 健康检查
`/积分` — uapis 积分额度
`/b站推送 add 群号 mid` — 盯 UP 主新投稿
`/全体 群号 内容` — 跨群 @全体

跨群管理：大部分命令可以用 `/<命令> 群号 参数` 的格式跨群操作，比如 `/kick 123456 @xxx`。

**不用前缀也能触发的：**

- "我要头衔 xxx" → 给自己设置专属头衔（只有 Bot 是群主的群生效，其他群静默忽略）
- "踢了 @xxx" / "把 @xxx 踢了" → 踢人
- "禁言 @xxx" / "把 @xxx 禁言了" → 禁言
- "解禁 @xxx" → 解禁
- "来看看" / "运势" → 今日运势
- "点歌 xxx" / "来首 xxx" → 搜歌
- 发 B站视频链接 / BV号 / b23 短链 → 自动解析并发出视频

## 命令权限标记

`bot/commands.py` 的 `register_all` 给每个命令打的权限标记，由 `bot/permission.py` 的 `check_permission` 统一校验。标记含义：

| 标记 | 含义 |
| --- | --- |
| `admin_only` | 调用者需要 QQ 群管理员/群主，或群内主人（master） |
| `bot_admin_required` | 机器人本人在该群必须是管理员或群主 |
| `bot_owner_required` | 机器人本人必须是 QQ 群主（头衔类操作，权限再高也绕不过 QQ 限制） |
| `bot_owner` | bot 主人、机器人账号本身，或群内主人（master）可用 |
| `bot_owner_only` | 仅 bot 主人 / 机器人账号本身可用 |

补充规则：bot 主人（`bot_owner` 配置的 QQ）和机器人账号（`bot_qq`）是 5 级 super，跳过一切校验；群内主人（master，4 级）跳过 `admin_only` 及之后的校验。

按标记分组（同一命令多个别名只列一次）：

| 标记组合 | 命令 |
| --- | --- |
| `bot_owner_only` | 好友列表、master、approve、reject |
| `bot_owner` | sysmsg、clearai、enable、disable、list、私聊ai、ai聊天、b站推送、积分 |
| `admin_only` | 安全、acg图、热榜推送、b站解析 |
| `admin_only` + `bot_admin_required` | 删除文件、新建文件夹、删除文件夹、移动文件、重命名文件、删公告、setgroupavatar、kick、ban、unban、allban、welcome、badword、精华、删精华、公告、admin、全体 |
| `admin_only` + `bot_owner_required` | title（头衔） |
| 无标记（所有群成员） | api、群信息、成员、成员列表、文件状态、图片描述、表情回应、戳、陌生人信息、help、like、rank、weather（天气）、translate、calc、fortune、ocr、转发摘要、群文件、文件链接、精华列表、群荣誉、已读、history、禁言列表、info、转发、点赞信息、health、生图、mytitle、热榜（热搜）、一言、答案之书、每日新闻、必应壁纸、epic免费 |

另外踢人/禁言类命令在执行时还会过 `can_moderate_target`：目标不能是 bot 主人或机器人账号（受保护），且操作者等级必须严格高于目标等级（super 除外）。

## 数据存哪

都在 `data/` 下面，已经在 `.gitignore` 里忽略了：

```
data/
├── memories/           短期记忆 + 长期摘要 + 按用户的记忆
├── stickers/           收集的表情包
├── blacklist.json      黑名单
├── uapi_state.json     uapis 积分用量
├── bili_push.json      UP 主推送已发记录
├── r18_warnings.json   R18 警告次数
├── security_events.json 安全事件记录
└── runtime_state.json  运行状态
```

## 已知问题 / 配置说明

以下配置键当前未生效（写在 config.json 里也不会被读取，暂时保留只是为了兼容旧配置）：

- `chat_limits.user_cooldown_seconds`
- `chat_limits.max_user_replies_per_10min`
- `runtime.ai_judge_min_gap_seconds`
- `group_defaults.features.interject`（插话目前由 AI 自行判断，不受此开关控制）
- `group_defaults.features.voice_reply`
- `sticker_mode.send_probability`（表情包发送由 AI 自主决定，不走概率）

另外：

- `memory_expire_hours`（顶层，可选，默认 72）：群聊工作记忆条目的过期小时数，已实际生效。
- `sticker_mode.max_stickers`（默认 50）：每个聊天上下文收集的表情包上限，已实际生效。

## 项目结构

```text
main.py                    稳定入口，委托给 app.bootstrap
app/
  config.py                配置加载、迁移和环境变量覆盖
  logging_setup.py         bot.log 与可选 chat.log 配置
  bootstrap.py             Client、Dispatcher 与后台任务生命周期
bot/
  ai/                      AI 提示词、Provider、记忆、搜索和工具
  commands/                管理、查询、媒体、娱乐、能力分类与动态帮助
  events/                  事件范围、路由、群聊和私聊处理
  transport/               OneBot WebSocket、消息段与长消息输出
  integrations/            Bilibili、TouchGal、UApiS、Mukyu、NapCat 实现
  services/                确认操作、调度、延迟回复、健康检查
  security/                URL 检查和灰字审计
  storage/                 原子 JSON 持久化
  dispatcher.py            运行状态和模块协调
  permission.py            五级身份与权限判断
```

`bot.client`、`bot.bilibili`、`bot.scheduler`、`bot.touchgal`、`bot.uapi`
保留为兼容入口；新代码使用聚焦后的 canonical 模块。详细说明见
`docs/architecture.md` 与 `docs/deployment.md`。

## 本次能力升级

- NapCat 接口契约基于 `4.18.13`，快照位于 `docs/api-contracts.json`。
- 纯文本超过 200 字自动改为合并转发；帮助菜单始终使用合并转发并引用引导。
- 帮助菜单按群聊/私聊、五级身份、Bot 群权限和功能开关动态生成。
- 最高主人明确呼叫小汐时始终回复；最高主人和配置群主人触发温柔、顺从、可爱的人格。
- 新增 `/消息`、`/群管`、`/待办`、`/相册`、`/文件`、`/好友`、`/账号`、`/互动`、`/自动化`、`/实验` 分类入口。
- 新增二维码、节假日、每日单词、GitHub、网址状态、敏感词、B站查询、云 OCR 和图片审核。
- B站投稿与动态自动推送在 Bot 为管理员或群主时会把 `@全体成员` 放在消息最前面。
## 运行可靠性

- 定时任务默认使用 `Asia/Shanghai` 时区，可通过 `runtime.scheduler_timezone` 调整。
- OneBot WebSocket 离线时，签到、ACG、热榜和 B站推送会跳过本轮，恢复连接后只执行未来任务。
- 新 ACG 图片使用 Mukyu `simple_json` 元数据和同源 `/i/...` URL，不消耗 UApiS 积分；定时收集始终请求 `r18=0`。
- B站官方接口出现 `-352` 或 `-412` 风控后默认暂停 30 分钟，可通过 `bilibili.risk_cooldown_seconds` 调整。
- 配置文件和 `config.json.last-good` 会使用 `0600` 权限保存，环境变量中的密钥不会写回配置文件。
- 生产环境建议使用 `deploy/qqbot.service`、以专用 `napcat` 用户运行的 `deploy/napcat.service`、常驻 `deploy/napcat-login-watchdog.service` 和受限的 `deploy/napcat-restart.path`，另配备份清理 timer，由 systemd 管理完整进程树。生产服务默认关闭聊天正文文件日志，并通过 NapCat 输出过滤器阻止消息正文和 token 进入 journald。
