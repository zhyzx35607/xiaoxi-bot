# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is a QQ group chatbot ("小汐") built on the OneBot v11 reverse WebSocket protocol. It connects to a [NapCat](https://github.com/NapNeko/NapCatQQ) client running on the same host and provides: slash commands, AI chat (DeepSeek), natural language triggers, group management, content moderation, sticker collection, and web search.

## Commands

```bash
# Start the bot (systemd service)
sudo systemctl start qqbot.service

# Check status
sudo systemctl status qqbot.service napcat.service

# View logs
tail -f /opt/qqbot/bot.log

# Run manually (stops the service first)
sudo systemctl stop qqbot.service && cd /opt/qqbot && python main.py

# There is no test suite / lint setup for this project.
```

## Architecture

```
main.py                        # Entry point: config load/migrate, start Client + Dispatcher, signal handling
└── bot/
    ├── client.py              # OneBot v11 WS client — connection lifecycle, all API calls, PID lock
    ├── dispatcher.py          # Central hub — routes events to handlers, AI chat gate logic, rate limiting
    ├── commands.py            # All slash-command handlers (/kick, /help, /fortune, etc.)
    ├── ai.py                  # DeepSeek LLM integration, persona, short/long-term memory, web search (Bing), sticker analysis
    ├── natural_triggers.py    # Pattern/phrase matching for commands without / prefix
    ├── notice_handler.py      # Group event handlers (join, leave, poke, admin changes, bad words, etc.)
    ├── permission.py          # Permission hierarchy: bot_owner > bot_qq > group masters > group admins > members
    ├── guard.py               # Blacklist and R18 warning system (per-group, time-expiring)
    ├── security.py            # URL safety checking (via NapCat check_url_safely), gray-tip audit
    ├── request_handler.py     # Friend/group join request storage and approve/reject flow
    ├── media.py               # Parse message segments: images (OCR+vision), forwards, voice, files
    ├── memory.py              # Extract user name/interest signals from messages (persistence via ai.py)
    ├── scheduler.py           # Optional periodic tasks (off by default — enable via runtime.enable_scheduler)
    └── utils.py               # atomic_write_json (tmpfile + os.replace)
```

## Key flows

**Message dispatch:** `main.py:amain()` → `Client.run()` (WS connect) → `Dispatcher.dispatch()` → routes by `post_type`:
- `message` → `_handle_message()` (group commands, AI chat, repeat detection, bad words, sticker collect)
- `notice` → `notice_handler.handle_notice()` (join/leave/poke/ban notices)
- `request` → `request_handler.handle_request()` (friend/group join requests)

**AI chat trigger:** Non-command group messages go through a decision gate (`_decide_ai_participation`):
1. Explicit triggers: @bot or name mention → always respond
2. Followup: user replied to within 120s of bot's last reply to them → low threshold
3. Interjection: scored by question detection, interest topics, group activity → threshold + probability

**Config:** `config.json` at repo root. Secrets (API keys, WS URL, token) should come from env vars, not the file — see `apply_env_overrides()` in main.py. The format uses `group_defaults` + per-group overrides under `groups`.

**Data directory:** JSON files under `data/`:
- `memories/group_*.json` — short-term AI memory (capped at 20, compressed to long-term)
- `memories/group_*_long.json` — long-term topic summaries (capped at 10)
- `stickers/group_*.json` — collected sticker metadata (capped at 50 per group)
- `blacklist.json`, `r18_warnings.json` — moderation state
- `security_events.json` — URL/gray-tip audit log

## Permission system

Defined in `permission.py`. Levels: `LEVEL_MASTER (4)` > `LEVEL_ADMIN (2)` > `LEVEL_MEMBER (1)`.

Command registration flags (in `commands.py:register_all`):
- `admin_only=True` — requires group admin/owner role
- `bot_admin_required=True` — bot must hold admin/owner in the group
- `bot_owner=True` — bot owner, bot_qq, or group masters
- `bot_owner_only=True` — only `config.bot_owner` (e.g., `/master`)
- `bot_owner_required=True` — bot must be group owner (e.g., `/title` for special titles)

The owner can issue cross-group commands from private chat by prefixing with a group ID.

## Environment variables

| Variable | Config key |
|---|---|
| `QQBOT_WS_URL` | `ws_url` |
| `QQBOT_TOKEN` / `ONEBOT_ACCESS_TOKEN` | `token` |
| `DEEPSEEK_API_KEY` / `QQBOT_DEEPSEEK_API_KEY` | `deepseek_api_key` |
| `DEEPSEEK_BASE_URL` | `deepseek_base_url` |
| `DEEPSEEK_MODEL` | `deepseek_model` |
| `VISION_API_KEY` / `QQBOT_VISION_API_KEY` | `vision_api.api_key` |
| `VISION_API_BASE_URL` | `vision_api.base_url` |
| `VISION_API_MODEL` | `vision_api.model` |
| `QQBOT_CONSOLE_LOG` | (enable console logging if `1`/`true`/`yes`) |

## Dependencies

- `websockets` — OneBot WS connection
- `aiohttp` — HTTP client for API calls (DeepSeek, vision, music search, Bing)
- Python 3.8+ stdlib (asyncio, json, logging, signal, fcntl)
