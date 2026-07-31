# QQ Bot Architecture

## Current migration boundary

The bot is being refactored in compatibility-preserving stages. Runtime
behavior, configuration paths, data files, and systemd entrypoints remain
unchanged during the migration.

The first completed boundary is the AI package:

```text
bot/ai/
├── __init__.py   compatibility facade for the historical bot.ai import
├── runtime.py    AI conversation orchestration and provider coordination
└── reply.py      reply tags, mention resolution, and OneBot segments
```

Existing imports such as `from bot.ai import handle_ai_chat` continue to work.
New code should import the narrowest module that owns the behavior.

## Target boundaries

Future stages will introduce these packages without changing public startup
interfaces:

- `bot/transport/`: OneBot WebSocket, message segments, and actions.
- `bot/events/`: normalized event context, routing, and event handlers.
- `bot/commands/`: command registry and domain-specific command modules.
- `bot/services/`: delayed replies, member cache, scheduler, and health.
- `bot/integrations/`: Bilibili, TouchGal, UAPI, and NapCat adapters.
- `bot/storage/`: JSON state and memory persistence.
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
