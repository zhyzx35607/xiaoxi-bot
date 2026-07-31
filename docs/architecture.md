# QQ Bot Architecture

## Migration boundary

The bot is refactored in compatibility-preserving stages. Runtime behavior,
configuration paths, data files, logs, environment variables, and systemd
entrypoints remain unchanged during migration.

The AI implementation is split into focused modules:

```text
bot/ai/
??? __init__.py   compatibility facade for the historical bot.ai import
??? runtime.py    AI conversation orchestration only
??? providers.py  provider failover, status, vision, and image generation
??? prompts.py    persona, timing, and system prompt construction
??? reply.py      reply tags, mention resolution, and OneBot segments
??? memory.py     working, user, and long-term memory persistence
??? stickers.py   sticker collection, analysis, and inventory
??? search.py     UAPI search and Bing fallback
??? tools.py      tool gating and multi-round tool execution
```

Existing imports such as `from bot.ai import handle_ai_chat` continue to work.
New code should import the narrowest module that owns the behavior.

Other established package boundaries:

- `bot/transport/`: OneBot WebSocket, message segments, and actions.
- `bot/events/`: normalized event routing and event handlers.
- `bot/commands/`: command registration and domain modules.
- `bot/services/`: delayed replies, member cache, scheduler, and health.
- `bot/integrations/`: Bilibili, TouchGal, UAPI, and NapCat adapters.
- `bot/storage/`: JSON state and persistence helpers.
- `app/`: configuration, logging, and process bootstrap.

## Compatibility rules

- Keep `main.py`, `/opt/qqbot`, `config.json`, `data/`, and systemd paths stable.
- Keep `bot.ai`, `bot.commands`, `bot.dispatcher`, and `bot.client` importable.
- Do not add third-party dependencies during structural migration.
- Separate behavior fixes from refactor commits.
- Run the full unittest suite before each deployment.

## Deployment rule

Each migration stage is a separate Git commit. Before deployment, create a
server backup, run the server virtual-environment tests, restart only the
affected service, verify OneBot connectivity, and retain a rollback target.
