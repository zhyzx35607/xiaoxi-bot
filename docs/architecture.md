# QQ Bot Architecture

## Runtime entry

`main.py` remains the production entrypoint. It configures logging through
`app.logging_setup`, then delegates startup and shutdown to `app.bootstrap`.
Configuration loading, recovery, migration, and environment overrides live in
`app.config`.

## Package boundaries

```text
app/
├── bootstrap.py       process lifecycle
├── config.py          config loading, migration, env overrides
└── logging_setup.py   bot.log and chat.log setup

bot/
├── ai/                prompts, providers, reply parsing, memory, stickers, search, tools
├── agent/             agent plans, tools, stores, verifier and background workers
├── roleplay/          roleplay cards, chats, world books, SQLite persistence
├── commands/          registry plus capability, admin, query, media and system domains
├── events/            context gates, routing, messages, notices, requests
├── transport/         OneBot WebSocket, actions, segments, long-output delivery
├── integrations/      Bilibili, TouchGal, UApiS, Mukyu, and NapCat implementations
├── services/          confirmations, scheduler, delayed replies, health/RSS guard
├── security/          URL checks and gray-tip audit implementation
├── storage/           atomic JSON persistence
└── dispatcher.py      state owner and external coordination facade
```

Root-level `ai_tools.py` (AI tool execution: permission checks and quotas),
`api_registry.py` (NapCat API capability registry) and `event_policy.py`
(event subscription policy) are active production modules; `actions.py` is a
compatibility wrapper.

Legacy modules such as `bot.client`, `bot.bilibili`, `bot.scheduler`,
`bot.touchgal`, and `bot.uapi` are compatibility proxies. Existing public
imports remain valid while canonical code uses the focused packages.

The Agent tool gateway (`bot/agent/tools/gateway.py`) exposes three layers:
read-only tools (registry reads, `SAFE_ACTIONS`, native read tools); low-risk
moderation (`delete_msg`, `set_group_ban`), which
executes only when the per-group switch, the bot's real-time group role,
target protection and the daily quota all pass; and high-risk moderation
(`set_group_kick`, `set_group_add_request`), which additionally requires a
human-confirmed plan
(`confirmed` metadata) or the super owner. Every layer fails closed.

## Stable public interfaces

```python
from bot.ai import handle_ai_chat
from bot.commands import register_all
from bot.dispatcher import Dispatcher
from bot.client import OneBotClient
```

The startup and runtime paths remain unchanged:

- `main.py`
- `/opt/qqbot`
- `/etc/qqbot.env`
- `config.json`
- `data/`
- `qqbot.service`

## Behavior-freeze rules

- Structural changes must not alter prompts, permissions, command behavior,
  message segments, configuration formats, or JSON data formats.
- New code imports the narrowest canonical module.
- Old import paths are removed only after a separately approved compatibility
  break; the current architecture intentionally retains them.
- No third-party dependency is added for structural refactoring.
- Every stage passes compile, full unittest, import compatibility, data-path,
  service startup, OneBot connection, Git cleanliness, and log checks.

## Deployment

See `docs/deployment.md` for the staged upload, isolated server test, restart,
verification, and rollback procedure.
