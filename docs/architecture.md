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
├── commands/          registry plus capability, admin, query, media and system domains
├── events/            context gates, routing, messages, notices, requests
├── transport/         OneBot WebSocket, actions, segments, long-output delivery
├── integrations/      Bilibili, TouchGal, UApiS, Mukyu, and NapCat implementations
├── services/          confirmations, scheduler, delayed replies, health/RSS guard
├── security/          URL checks and gray-tip audit implementation
├── storage/           atomic JSON persistence
└── dispatcher.py      state owner and external coordination facade
```

Legacy modules such as `bot.client`, `bot.bilibili`, `bot.scheduler`,
`bot.touchgal`, and `bot.uapi` are compatibility proxies. Existing public
imports remain valid while canonical code uses the focused packages.

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
