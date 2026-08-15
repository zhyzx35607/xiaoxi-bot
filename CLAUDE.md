# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working on this repository.

## Overview

小汐 is a QQ group chatbot built on the OneBot v11 reverse WebSocket protocol. It connects to a local [NapCat](https://github.com/NapNeko/NapCatQQ) client and provides slash commands, AI chat, natural language triggers, group management, content moderation, sticker collection, and web search.

## Commands

```bash
# Start/stop/status
sudo systemctl start qqbot.service
sudo systemctl status qqbot.service napcat.service
sudo systemctl restart qqbot.service

# View logs
sudo journalctl -u qqbot.service -f
sudo tail -f /var/log/qqbot/bot.log
sudo tail -f /var/log/qqbot/chat.log

# Run manually (stop service first; MUST load env or NapCat kicks the WS for missing token)
sudo systemctl stop qqbot.service && cd /opt/qqbot && set -a && source /etc/qqbot.env && set +a && ./venv/bin/python main.py

# Run tests
./venv/bin/python -m unittest discover -s tests -t . -v
```

## Architecture

```text
main.py                    # Stable entrypoint
app/                       # Config, logging, process bootstrap
bot/ai/                    # AI orchestration and focused subsystems
bot/commands/              # Registry and domain command modules
bot/events/                # Context, router, message, notice, request
bot/transport/             # OneBot client, actions, segments
bot/integrations/          # Bilibili, TouchGal, UAPI implementations
bot/services/              # Scheduler, delayed reply, member cache, health
bot/storage/               # Atomic JSON persistence
bot/dispatcher.py          # State owner and compatibility coordinator
```

Historical imports remain supported through compatibility facades. New code
should import canonical focused modules. See `docs/architecture.md` and
`docs/deployment.md`.

## Key flows

**Message dispatch:** `main.py:amain()` → `Client.run()` (WS connect) → `Dispatcher.dispatch()` → routes by `post_type`:
- `message` → `_handle_message()` (group commands, AI chat, repeat detection, bad words, sticker collect)
- `message_sent` → `_handle_self_message()` (own messages: context buffer + fixed commands from the bot account; requires NapCat `reportSelfMessage: true`, which is set on the port-3001 WS server)
- `notice` → `notice_handler.handle_notice()` (join/leave/poke/ban notices)
- `request` → `request_handler.handle_request()` (friend/group join requests)

**AI chat gate (group):** Hard filters + AI autonomy:
1. **Explicit triggers** (`@bot` or name mention) → reply immediately, with 10s group / 15s user cooldown.
2. **Follow-up** → user replied within 120s of bot's last reply to them → reply immediately.
3. **Interjection candidate** → passes cheap filters (not too short, not pure sticker, not sleep hours, etc.) → enters a lightweight delayed-reply heap queue (60–300s). At fire time the bot rebuilds the latest context and asks the AI whether to reply or `[SKIP]`.

**Private chat:** Gated by `private_chat` config (`enabled` default **false** + `allowed_users` list, toggled via `/私聊AI`). The bot owner always passes. Non-friends are silently ignored (no "add friend first" reply). No hard rate limit; the AI decides whether to reply, how long, and when to stop, guided by the persona prompt.

**Persona / prompts:** See `bot/ai/` (prompts live in `bot/ai/prompts.py`). The system prompt is layered:
- `PERSONA_PROFILE` — identity and background facts
- `STYLE_RULES` — speaking style, boundaries, tone
- `TIMING_RULES` — when to speak vs. `[SKIP]`
- `OUTPUT_PROTOCOL` — newline-delimited multi-message, `[STICKER:...]`, `[REPLY]`, `[AT:nick]`, `[POKE:nick]`
- Dynamic context — Beijing time/schedule, image summary, web search, memory, chat history

**Sending pipeline:** AI output is split by newlines (max 3 segments), each segment sent as a separate message with a length-proportional typing delay (0.5–1.5s base + 0.08s/char, capped at 8s).

**Voice replies:** Removed. NapCat's `get_ai_characters` timed out in testing; the bot does not send `[VOICE]`.

**Image/sticker handling:** Normal images are described via the vision API. Sticker/emoji collection (`collect_sticker_async`) only collects images whose `sub_type != "0"`, skipping normal photos.

**Config:** `config.json` at repo root. Secrets (API keys, WS URL, token) must come from env vars, not the file — see `apply_env_overrides()` in `app/config.py`. The format uses `group_defaults` + per-group overrides under `groups`.

**Data directory:** JSON files under `data/`:
- `memories/group_*.json` — short-term AI memory (capped at 20)
- `memories/group_*_long.json` — long-term topic summaries (capped at 10)
- `memories/group_*_u*.json` — per-user per-group memory
- `stickers/group_*.json` — collected sticker metadata (capped at 50 per group)
- `blacklist.json`, `r18_warnings.json` — moderation state
- `security_events.json` — URL/gray-tip audit log
- `runtime_state.json` — daily counters (likes, fortunes, message counts)

## Permission system

Defined in `permission.py`. Five tiers: `LEVEL_SUPER (5, bot_owner + bot_qq)` > `LEVEL_MASTER (4, per-group masters)` > `LEVEL_GOWNER (3, QQ group owner)` > `LEVEL_ADMIN (2)` > `LEVEL_MEMBER (1)`. The bot account itself is super; group masters can only be added/removed by super (`/master`).

Command registration flags (in `bot/commands/registry.py:register_all`):
- `admin_only=True` — requires group admin/owner role
- `bot_admin_required=True` — bot must hold admin/owner in the group
- `bot_owner=True` — bot owner, bot_qq, or group masters
- `bot_owner_only=True` — only `config.bot_owner` or `bot_qq` (e.g., `/master`)
- `bot_owner_required=True` — bot must be group owner (e.g., `/title` for special titles)

The owner can issue cross-group commands from private chat by prefixing with a group ID.

## uapis.cn credit budget

`bot/uapi.py`（兼容 facade，实现在 `bot/integrations/uapi.py`）applies local command/automation protection buckets while recording actual UApiS debits from `Uapi-Credits-Charged`. Official monthly remaining quota is parsed from rate-limit response headers and persisted in `data/uapi_state.json`. Requests without a key use visitor quota; free endpoints retry without authentication if a configured key is rejected.

## Bilibili integration

`bot/bilibili.py`（兼容 facade，实现在 `bot/integrations/bilibili.py`）: group messages containing BV/av/b23 links are auto-parsed (official `x/web-interface/view`, anonymous `platform=html5` playurl for mp4 download, ≤80MB streamed to `tmp/`, sent as video segment, then deleted). Per-group UP主 watch list (`groups.<gid>.bili_push.mids`, managed via `/b站推送`) is polled every 60s via wbi-signed `x/space/wbi/arc/search` (buvid cookie + wbi keys refreshed ~12h). Datacenter IPs get intermittent -352/-412 risk-control responses, so the poller uses a bounded retry count (`bilibili.official_retries`, default 2), then opens a cooldown circuit (`bilibili.risk_cooldown_seconds`, default 1800) before optionally falling back to uapis.cn `social/bilibili/archives`. Announced bvids are persisted in `data/bili_push.json`; adding a mid primes the seen-list to avoid flooding.

## Environment variables

| Variable | Config key | Notes |
|---|---|---|
| `QQBOT_WS_URL` | `ws_url` | |
| `QQBOT_TOKEN` / `ONEBOT_ACCESS_TOKEN` | `token` | Never write to `config.json` |
| `SIGMAI_API_KEY` / `QQBOT_SIGMAI_API_KEY` | `sigmai_api_key` | Primary chat provider |
| `SIGMAI_BASE_URL` | `sigmai_base_url` | Default: `https://www.sigmai.net/v1` |
| `SIGMAI_MODEL` | `sigmai_model` | Default: `DeepSeek-V4-Flash` |
| `DEEPSEEK_API_KEY` / `QQBOT_DEEPSEEK_API_KEY` | `deepseek_api_key` | Fallback provider |
| `DEEPSEEK_BASE_URL` | `deepseek_base_url` | Default: `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | `deepseek_model` | Default: `deepseek-chat` |
| `AGNES_API_KEY` / `QQBOT_AGNES_API_KEY` | `agnes_api_key` | Retained **only** for `/生图` |
| `VISION_API_KEY` / `QQBOT_VISION_API_KEY` | `vision_api.api_key` | Image understanding (Aliyun qwen-vl-plus) |
| `VISION_API_BASE_URL` | `vision_api.base_url` | |
| `VISION_API_MODEL` | `vision_api.model` | |
| `UAPI_API_KEY` / `QQBOT_UAPI_API_KEY` | `uapi_api_key` | uapis.cn Bearer key (fun commands, B站 fallback) |
| `MUKYU_API_KEY` / `QQBOT_MUKYU_API_KEY` | `mukyu_api_key` | Optional key for the Mukyu random image service |
| `BILI_SESSDATA` / `QQBOT_BILI_SESSDATA` | `bili_sessdata` | Optional B站 login cookie; makes official endpoints risk-control-free (near-100% reliable UP主 polling, zero credits). Expiry detected via nav `code=-101`, falls back to anonymous |
| `QQBOT_CONSOLE_LOG` | — | Enable console logging if `1`/`true`/`yes` |

## Testing

The `unittest` regression suite lives under `tests/`. Run it with:

```bash
./venv/bin/python -m unittest discover -s tests -t . -v
```

Tests cover: chat logging, tail reader, API result normalization, NapCat tool gate, scheduler midnight math, private typing lifecycle, AI outage notices, friend cache throttling, DeepSeek/SigmaI provider status, manual/daily checkin, message segmentation, `[SKIP]` signal, typing delay bounds, delayed queue merge/cap, bad-word union merging, and config secret stripping.

## Dependencies

- Python 3.10+
- Install the pinned runtime set with `pip install -r requirements.txt`
- `websockets` — OneBot WS connection
- `aiohttp` — HTTP client for API calls (SigmaI, DeepSeek, vision, music search, Bing)

## Notes for maintainers

- The current target server has 8 GB RAM. Keep the bot lightweight because NapCat/QQ normally uses over 1 GB and media forwarding can temporarily increase memory and I/O.
- Do not add new secrets to `config.json`; use env vars or `permission.py:save_group_config` will strip them.
- The delayed-reply queue is intentionally in-memory only (cap 20). Lost entries on restart are acceptable and match the "human" persona.
- The AI persona is the primary behavior controller; avoid adding more hard-coded rules for social timing. Hard-code only safety/permission boundaries.
- AI tools (`ai_tools.py`): management APIs are never exposed; interaction tools (`set_msg_emoji_like`, `send_like`) are only offered in explicit/follow-up scenes and hard-capped at 30/group/day (`INTERACTION_DAILY_LIMIT`).
- An RSS watchdog (`dispatcher.start_rss_guard`) logs memory growth and triggers a graceful SIGTERM restart at `runtime.rss_restart_mb` (default 700 MB, below the current systemd `MemoryMax=1G`).
- `tmp/` holds transient video/image downloads; files are deleted right after sending.
- Bilibili push state advances only after a confirmed send. Ambiguous timeouts are checked against recent group history before the next retry.
- ACG content uses Mukyu safe-only image metadata (`r18=0`) and same-origin image URLs, with a persistent 20-image pool and seven-day URL deduplication. Random daily windows and pending per-group deliveries persist in `data/acg_history.json`.


## Runtime reliability rules

- Scheduler wall-clock calculations default to `Asia/Shanghai` through `runtime.scheduler_timezone`.
- Background jobs must check `dispatcher.client.is_connected` before calling OneBot.
- ACG URL resolution requires `uapi_api_key`; pending delivery retries remain allowed without a key.
- Bilibili `-352`/`-412` responses activate a shared risk-control cooldown instead of per-UP retry storms.
- `main.load_config()` maintains `config.json.last-good` and restores it after JSON corruption.
- Production systemd units are versioned under `deploy/`; NapCat must use `KillMode=control-group` without a custom PID-file `ExecStop`.
