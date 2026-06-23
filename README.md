# 小汐 — QQ 群聊机器人

一个跑在 [NapCat](https://github.com/NapNeko/NapCatQQ) 上的 QQ 机器人，OneBot v11 协议。

小汐的人设是一个 20 岁的女大学生，爱刷手机爱追番。她能在群里闲聊接话、帮忙管群、收表情包、认图片里的字，私聊也能聊。后端用的 DeepSeek，费用很低，几块钱能用好久。

## 她能干什么

**聊天方面：**
- 群里 @她或者叫"小汐"，她就会回你
- 有时候她会自己判断语境插话——比如有人在问问题、聊到她懂的话题，她可能就冒出来了
- 遇到她不确定的事实性问题，会自动去网上搜一下再回答（用的 Bing，免费）
- 能看懂图片：你发张图她可以说说是什么，也能 OCR 提取上面的文字（优先用 Qwen 识图，QQ 自带摘要做备选）
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
- 表情包：自动收集群里的表情，偶尔发出贴合语境的表情包
- 点歌：说"来首 xxx"就能搜

**私聊：**
- 好友私聊可以跟 AI 自由聊天，非好友会提示先加好友
- Bot 主人可以在私聊里用管理命令、看日志、处理加群申请

## 怎么跑起来

先装 NapCat，让它开个 WebSocket 服务端监听 `ws://127.0.0.1:3001`。

然后：

```bash
git clone https://github.com/zhyzx35607/xiaoxi-bot.git
cd xiaoxi-bot
python3 -m venv venv
source venv/bin/activate
pip install websockets aiohttp
```

配置就靠环境变量，密钥不要写进 config.json：

```bash
export DEEPSEEK_API_KEY="sk-xxxxxxxxxxxxxxxx"    # DeepSeek 的 key，注册就有
export QQBOT_WS_URL="ws://127.0.0.1:3001"        # NapCat 的 WS 地址
export QQBOT_TOKEN=""                             # OneBot access token，没设就不填
```

如果要用图片识别功能，再配 Vision API（可选）：

```bash
export VISION_API_KEY="sk-xxxxxxxxxxxxxxxx"
export VISION_API_BASE_URL="https://your-api.com/v1"
export VISION_API_MODEL="qwen-vl-plus"
```

启动：

```bash
python main.py
```

用 systemd 托管更稳：

```bash
sudo cp qqbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qqbot.service
```

config.json 里可以按群开关各种功能，调整 AI 插话的积极性、频率限制等等。用到的时候看看文件里的注释就行。

## 命令一览

群里用 `/` 前缀，也有些话不带前缀也能触发。

**所有人都能用：**

`/help` — 看有哪些命令
`/fortune` — 今日运势
`/like @xxx` — 点赞
`/rank` — 发言排行
`/weather 城市` — 天气
`/translate 文本` — 翻译
`/calc 1+2*3` — 计算器
`/ocr` — 识别图片文字（回复那张图发）
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

**群主才能用的：**

`/title @xxx 头衔` — 设专属头衔（Bot 得是群主）
`/enable` `/disable` — 开关本群的 Bot 功能
`/clearai` — 清掉本群的 AI 记忆和表情包

**Bot 主人的私聊命令（在私聊窗口发给 Bot）：**

`/status` — 看运行状态、内存、在线时间
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

跨群管理：大部分命令可以用 `/<命令> 群号 参数` 的格式跨群操作，比如 `/kick 123456 @xxx`。

**不用前缀也能触发的：**

- "踢了 @xxx" / "把 @xxx 踢了" → 踢人
- "禁言 @xxx" / "把 @xxx 禁言了" → 禁言
- "解禁 @xxx" → 解禁
- "来看看" / "运势" → 今日运势
- "点歌 xxx" / "来首 xxx" → 搜歌

## 代码结构

```
main.py                    入口，负责启动、配置迁移、信号处理
bot/
 ├── client.py             OneBot WS 连接、所有 API 调用
 ├── dispatcher.py         事件调度中心，AI 插话决策也在这
 ├── commands.py           所有 / 命令的处理逻辑
 ├── ai.py                 DeepSeek 调用、人设、记忆、联网搜索
 ├── natural_triggers.py   自然语言触发（不带 / 的命令）
 ├── notice_handler.py     群事件：加群退群、戳一戳、违禁词等
 ├── permission.py         权限判断
 ├── guard.py              黑名单和 R18 警告
 ├── security.py           链接安全和灰条审计
 ├── request_handler.py    好友/加群申请的存取
 ├── media.py              消息解析：图片 OCR、转发、语音、文件
 ├── memory.py             从聊天里提取用户信息
 ├── scheduler.py          定时任务（默认关着）
 └── utils.py              原子化写 JSON 的工具
```

## 数据存哪

都在 `data/` 下面，已经在 `.gitignore` 里忽略了：

```
data/
├── memories/           短期记忆 + 长期摘要 + 按用户的记忆
├── stickers/           收集的表情包
├── blacklist.json      黑名单
├── r18_warnings.json   R18 警告次数
├── security_events.json 安全事件记录
└── runtime_state.json  运行状态
```

## 服务器要求

我自己跑在阿里云 1.6G 内存的机器上，Bot 本身只占三四十兆，但 NapCat 的 QQ 客户端要吃掉五六百兆。建议至少 1.5G 内存。

Python 3.8 就行，依赖就 `websockets` 和 `aiohttp` 两个包。

---

有问题提 issue 就行。
