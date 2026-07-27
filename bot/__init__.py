"""QQ Bot ("小汐") — OneBot v11 reverse-WS client for NapCat.

Modules:
    client      — WebSocket connection + OneBot API calls
    dispatcher  — Event routing, AI chat gate, rate limiting
    commands    — Slash-command handlers
    ai          — DeepSeek LLM persona, memory, web search, stickers
    permission  — 5-level access control (super > master > group-owner > admin > member)
    guard       — Blacklist & R18 warning system
    security    — URL safety check & gray-tip audit
    media       — Image OCR, forward-description, file parsing
    memory      — Extract user name/interest signals from messages
    scheduler   — Timed jobs (check-in, ACG images, hot-board push)
    uapi        — uapis.cn client with credit budget
    bilibili    — B站 video parse/download + UP主 push
    utils       — atomic_write_json helper
"""
