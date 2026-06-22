"""QQ Bot ("小汐") — OneBot v11 reverse-WS client for NapCat.

Modules:
    client      — WebSocket connection + OneBot API calls
    dispatcher  — Event routing, AI chat gate, rate limiting
    commands    — Slash-command handlers
    ai          — DeepSeek LLM persona, memory, web search, stickers
    permission  — 4-level access control (owner > master > admin > member)
    guard       — Blacklist & R18 warning system
    security    — URL safety check & gray-tip audit
    media       — Image OCR, forward-description, file parsing
    memory      — Extract user name/interest signals from messages
    scheduler   — Optional periodic tasks (off by default)
    utils       — atomic_write_json helper
"""
