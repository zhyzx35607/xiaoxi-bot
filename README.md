# 小汐 (Xiao Xi) — QQ 群聊机器人

基于 [OneBot v11](https://github.com/botuniverse/onebot-11) 反向 WebSocket 协议，连接 [NapCat](https://github.com/NapNeko/NapCatQQ) 客户端的多功能 QQ 群聊机器人。

小汐是一个 20 岁女大学生人设的 AI 群友，能自然参与群聊、执行管理命令、收集表情包、识别图片内容，还能在私聊中与你聊天。

## 功能特性

### 🤖 AI 聊天
- **DeepSeek 驱动**：自然语言理解与生成，像真人一样参与群聊
- **多级触发**：@机器人 / 叫名字"小汐" / 智能判断语境自动插话 / 追问链式对话
- **联网搜索**：自动检测事实类问题，通过 Bing 搜索核实后回答
- **图片理解**：支持 OCR 文字识别 + Vision API 图片内容描述
- **合并转发**：能阅读合并转发消息并参与讨论
- **语音消息**：能识别语音消息（转文字）

### 🛡️ 群管理
- **入群欢迎**：自定义欢迎语模板，支持 `{nickname}` 占位符
- **违禁词检测**：支持普通匹配和正则匹配，自动撤回 + 警告
- **黑名单系统**：按群隔离，时间到期自动解除
- **R18 内容拦截**：AI 自动识别色情/骚扰内容，三次警告自动拉黑 48 小时
- **URL 安全检查**：调用 QQ 安全接口检测链接，危险链接自动撤回 + 禁言

### 🎮 互动功能
- **今日运势**：AI 生成趣味运势（每人每天一次）
- **点赞排行**：统计群内发言量排行
- **戳一戳**：被戳自动回戳
- **点赞秒回**：任何人给你的资料卡点赞，一秒内回赞满（SVIP 20 次，普通 10 次）
- **复读机**：群友重复同一句话达到阈值，概率跟风复读
- **点歌**：自然语言触发音乐搜索

### 📦 表情包管理
- **自动收集**：群内图片自动存入表情库
- **智能标签**：可选 AI 视觉分析，自动打标签、分类、标注适用场景
- **上下文发送**：AI 回复后可选择最合适的表情包追加发送

### 💬 私聊功能
- **AI 聊天**：任何好友私聊都能和 AI 自由聊天
- **管理面板**：Bot 主人在私聊中可使用全部管理命令，支持跨群操作
- **限流保护**：非主人私聊限制 20 条/10 分钟

### 🔐 权限系统
五级权限层级：`Bot 主人` > `Bot QQ 号` > `群主(Level 4)` > `群管理员(Level 2)` > `群成员(Level 1)`

命令可按角色控制访问：
- `admin_only` — 需要群内管理员身份
- `bot_admin_required` — Bot 自身需要是群管理员
- `bot_owner_required` — Bot 自身需要是群主
- `bot_owner` — Bot 主人、Bot QQ 或群主
- `bot_owner_only` — 仅 Bot 主人

## 架构

```
main.py                        # 入口：配置加载/迁移，启动 Client + Dispatcher，信号处理
└── bot/
    ├── client.py              # OneBot v11 WS 客户端 — 连接管理、所有 API 封装、PID 锁
    ├── dispatcher.py          # 中央调度器 — 事件路由、AI 聊天门控、频率限制
    ├── commands.py            # 斜杠命令处理器 (/kick, /help, /fortune 等)
    ├── ai.py                  # DeepSeek LLM 集成、人设、短/长期记忆、联网搜索、表情分析
    ├── natural_triggers.py    # 无前缀自然语言触发（"踢了"、"禁言"等）
    ├── notice_handler.py      # 群事件处理（加群、退群、戳一戳、管理员变更、违禁词等）
    ├── permission.py          # 权限层级系统
    ├── guard.py               # 黑名单与 R18 警告系统（按群隔离、时间过期）
    ├── security.py            # URL 安全检查、灰条审计日志
    ├── request_handler.py     # 好友/群加群请求存储与审批流
    ├── media.py               # 消息分段解析：图片(OCR+视觉)、转发、语音、文件
    ├── memory.py              # 从聊天中提取用户名称、兴趣等长期记忆信号
    ├── scheduler.py           # 可选定时任务（默认关闭）
    └── utils.py               # 原子化 JSON 写入工具
```

## 快速开始

### 前置条件

1. **NapCat QQ 客户端**：[安装 NapCat](https://github.com/NapNeko/NapCatQQ)，配置 WebSocket 服务端监听 `ws://127.0.0.1:3001`
2. **Python 3.8+** 和虚拟环境
3. **DeepSeek API Key**：[DeepSeek 开放平台](https://platform.deepseek.com/) 注册获取
4. （可选）**Vision API**：用于图片理解，支持 OpenAI 兼容接口

### 安装

```bash
# 克隆仓库
git clone https://github.com/zhyzx35607/xiaoxi-bot.git
cd xiaoxi-bot

# 创建虚拟环境并安装依赖
python3 -m venv venv
source venv/bin/activate
pip install websockets aiohttp

# 创建配置（从模板）
cp config.example.json config.json
# 编辑 config.json 填入群号和功能开关
```

### 配置环境变量

API 密钥通过环境变量加载，**不要写入 config.json**：

```bash
export DEEPSEEK_API_KEY="sk-xxxxxxxxxxxxxxxx"
export QQBOT_WS_URL="ws://127.0.0.1:3001"      # NapCat WebSocket 地址
export QQBOT_TOKEN=""                            # OneBot access_token（如需要）
export VISION_API_KEY="sk-xxxxxxxxxxxxxxxx"      # 可选：视觉识别 API
export VISION_API_BASE_URL="https://your-api.com/v1"  # 可选
export VISION_API_MODEL="qwen-vl-plus"           # 可选
```

### 运行

```bash
# 直接运行（会停止已有的 systemd 服务）
sudo systemctl stop qqbot.service
python main.py

# 作为 systemd 服务运行
sudo cp qqbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qqbot.service
```

## 命令列表

### 通用命令（所有群成员可用）

| 命令 | 说明 |
|------|------|
| `/help` | 查看可用命令 |
| `/fortune` | 今日运势 |
| `/like @用户` | 给用户点赞 |
| `/rank` | 群内发言排行 |
| `/weather 城市` | 查询天气 |
| `/translate 文本` | 翻译文本 |
| `/calc 表达式` | 计算器（如 `/calc 1+2*3`） |
| `/ocr` | 识别图片文字（回复图片使用） |
| `/转发摘要` | 总结合并转发内容 |
| `/info [@用户或QQ号]` | 查看成员信息 |
| `/history [数量]` | 查看最近消息 |
| `/精华列表` | 查看群精华消息 |
| `/群荣誉` | 查看群荣誉信息 |
| `/群文件 [关键词]` | 查看/搜索群文件 |
| `/文件链接 file_id busid` | 获取群文件的下载链接 |
| `/禁言列表` | 查看当前被禁言的群友 |
| `/已读` | 标记消息为已读 |
| `/转发` | 转发消息（需回复目标消息） |
| `/点赞信息` | 查看机器人的点赞统计 |

### 管理命令（需群管理员 + Bot 有管理权限）

| 命令 | 说明 |
|------|------|
| `/kick @用户` | 踢出群成员 |
| `/ban @用户 [分钟]` | 禁言群成员（默认 30 分钟） |
| `/unban @用户` | 解除禁言 |
| `/allban on/off` | 全员禁言开关 |
| `/welcome` | 入群欢迎语设置 |
| `/badword` | 违禁词设置（支持普通匹配和正则 `re:xxx`） |
| `/admin add/del @用户` | 设置/取消群管理员 |
| `/精华` | 将回复的消息设为精华 |
| `/删精华` | 删除精华消息 |
| `/公告` | 发布/查看群公告 |
| `/setgroupavatar` | 设置群头像（回复图片使用） |
| `/安全 status/log` | 查看安全功能状态和日志 |

### 群主专属命令

| 命令 | 说明 |
|------|------|
| `/title @用户 头衔` | 设置专属头衔（需 Bot 为群主） |
| `/enable` / `/disable` | 开启/关闭本群机器人功能 |
| `/clearai` | 清除本群 AI 记忆和表情包数据 |

### Bot 主人私聊命令（在私聊中发送给机器人）

| 命令 | 说明 |
|------|------|
| `/status` | 查看运行状态、内存、在线时长 |
| `/list` | 查看所有群组数据概览 |
| `/log [N]` | 查看最近 N 条日志 |
| `/bl list/add/remove` | 黑名单管理 |
| `/group enable/disable/list 群号` | 群组开关管理 |
| `/memory 群号` | 查看群的 AI 记忆 |
| `/memory clear 群号` | 清除群的 AI 记忆 |
| `/sticker 群号` | 查看表情包数量 |
| `/sticker clear 群号` | 清除表情包记录 |
| `/sysmsg` | 查看入群申请/邀请 |
| `/approve flag尾号` | 通过加群申请 |
| `/reject flag尾号 原因` | 拒绝加群申请 |
| `/clearai 群号` | 清除群的完整数据 |
| `/health` | 运行健康检查 |

私聊跨群管理：大部分管理命令可通过 `/<命令> 群号 参数` 格式跨群操作。

### 自然语言触发（无需 `/` 前缀）

| 说法 | 效果 |
|------|------|
| "踢了 @某人" / "把 @某人 踢了" | 踢出成员 |
| "禁言 @某人" / "把 @某人 禁言了" | 禁言成员 |
| "解禁 @某人" | 解除禁言 |
| "来看看"/"来测测"/"运势" | 今日运势 |
| "点歌 歌名" / "来首 歌名" | 音乐搜索 |

## 配置说明

### config.json 结构

```json
{
  "bot_qq": 机器人QQ号,
  "bot_owner": 主人QQ号,
  "group_defaults": { /* 新群的默认设置 */ },
  "groups": {
    "群号": {
      "enabled": true,
      "masters": [],
      "features": {
        "ai_chat": true,        // AI 聊天
        "interject": true,      // 智能插话
        "repeat": true,         // 复读机
        "music": true,          // 点歌
        "fortune": true,        // 运势
        "admin_cmds": true,     // 管理命令
        "voice_reply": false,   // AI 语音回复
        "auto_poke": true,      // 自动回戳
        "auto_essence": false   // 自动设精华
      }
    }
  },
  "runtime": {
    "ai_concurrency": 1,        // AI 并发数（低配服务器建议 1）
    "enable_scheduler": false,  // 定时任务（低配建议关）
    "ws_queue_size": 50         // WS 消息队列大小
  }
}
```

所有敏感字段（API 密钥、Token）必须通过环境变量设置，见上方「配置环境变量」。

## 环境变量参考

| 变量 | 对应配置 | 说明 |
|------|----------|------|
| `DEEPSEEK_API_KEY` | `deepseek_api_key` | DeepSeek API 密钥（必填） |
| `QQBOT_DEEPSEEK_API_KEY` | `deepseek_api_key` | 同上（备选变量名） |
| `DEEPSEEK_BASE_URL` | `deepseek_base_url` | DeepSeek API 地址（默认官方） |
| `DEEPSEEK_MODEL` | `deepseek_model` | 模型名（默认 deepseek-chat） |
| `QQBOT_WS_URL` | `ws_url` | NapCat WebSocket 地址 |
| `QQBOT_TOKEN` | `token` | OneBot access_token |
| `ONEBOT_ACCESS_TOKEN` | `token` | 同上（备选变量名） |
| `VISION_API_KEY` | `vision_api.api_key` | 视觉识别 API 密钥（可选） |
| `VISION_API_BASE_URL` | `vision_api.base_url` | 视觉 API 地址 |
| `VISION_API_MODEL` | `vision_api.model` | 视觉模型名 |
| `QQBOT_CONSOLE_LOG` | — | 设为 `1` 开启控制台日志 |

## 数据存储

数据保存在 `data/` 目录下（已在 `.gitignore` 中排除）：

```
data/
├── memories/           # AI 对话记忆（短期 20 条上限，长期 10 条上限）
│   ├── group_*.json
│   ├── group_*_long.json
│   └── group_*_u*.json  # 用户个人记忆
├── stickers/           # 表情包收集
│   └── group_*.json
├── blacklist.json      # 黑名单
├── r18_warnings.json   # R18 警告计数
├── security_events.json # 安全事件日志
└── runtime_state.json  # 运行时状态（每日重置）
```

## 系统要求

| 项目 | 最低配置 | 推荐配置 |
|------|----------|----------|
| CPU | 1 核 | 2 核 |
| 内存 | 512MB（仅 bot）| 1.5GB+（含 NapCat QQ） |
| 磁盘 | 100MB | 1GB |
| Python | 3.8+ | 3.10+ |

> **注意**：NapCat QQ 客户端本身需要约 500MB-900MB 内存。整体部署建议 1.5GB 以上内存的服务器。

## License

MIT

---

**Contributors**: [zhyzx35607](https://github.com/zhyzx35607)
