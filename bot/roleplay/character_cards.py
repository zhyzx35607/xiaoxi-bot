"""SillyTavern character-card parsing helpers."""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path
from typing import Any


class CharacterCardError(ValueError):
    pass


def _normalize_card(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    name = str(data.get("name") or payload.get("name") or "").strip()
    if not name:
        raise CharacterCardError("角色卡缺少 name")
    alternates = data.get("alternate_greetings") or []
    if not isinstance(alternates, list):
        alternates = []
    tags = data.get("tags") or payload.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    card = {
        "spec": str(payload.get("spec") or "chara_card_v2"),
        "spec_version": str(payload.get("spec_version") or "2.0"),
        "name": name[:120],
        "description": str(data.get("description") or "")[:20000],
        "personality": str(data.get("personality") or "")[:8000],
        "scenario": str(data.get("scenario") or "")[:12000],
        "first_mes": str(data.get("first_mes") or data.get("first_message") or "")[:12000],
        "mes_example": str(data.get("mes_example") or "")[:16000],
        "creator_notes": str(data.get("creator_notes") or "")[:8000],
        "system_prompt": str(data.get("system_prompt") or "")[:8000],
        "post_history_instructions": str(data.get("post_history_instructions") or "")[:8000],
        "alternate_greetings": [str(v)[:4000] for v in alternates[:20]],
        "tags": [str(v)[:80] for v in tags[:50]],
        "creator": str(data.get("creator") or "")[:200],
        "character_version": str(data.get("character_version") or "")[:80],
        "extensions": data.get("extensions") if isinstance(data.get("extensions"), dict) else {},
    }
    return card


def parse_json_card(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8-sig")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CharacterCardError(f"角色卡 JSON 无效: {exc}") from exc
    if not isinstance(payload, dict):
        raise CharacterCardError("角色卡根节点必须是对象")
    return _normalize_card(payload)


def _png_text_chunks(data: bytes) -> dict[str, str]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise CharacterCardError("不是有效 PNG")
    offset = 8
    result: dict[str, str] = {}
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        offset += 12 + length
        if kind == b"tEXt" and b"\x00" in payload:
            key, value = payload.split(b"\x00", 1)
            result[key.decode("latin1", "ignore")] = value.decode("latin1", "ignore")
        elif kind == b"iTXt" and b"\x00" in payload:
            parts = payload.split(b"\x00", 5)
            if len(parts) == 6:
                result[parts[0].decode("latin1", "ignore")] = parts[5].decode("utf-8", "ignore")
        if kind == b"IEND":
            break
    return result


def parse_png_card(data: bytes) -> dict[str, Any]:
    chunks = _png_text_chunks(data)
    encoded = chunks.get("chara") or chunks.get("ccv3")
    if not encoded:
        raise CharacterCardError("PNG 中没有角色卡元数据")
    try:
        decoded = base64.b64decode(encoded)
    except Exception as exc:
        raise CharacterCardError("PNG 角色卡元数据不是有效 Base64") from exc
    return parse_json_card(decoded)


def load_character_card(path: str | Path) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    if not target.is_file():
        raise CharacterCardError("角色卡文件不存在")
    if target.stat().st_size > 10 * 1024 * 1024:
        raise CharacterCardError("角色卡文件超过 10MB")
    if target.suffix.lower() == ".png":
        return parse_png_card(target.read_bytes())
    if target.suffix.lower() in {".json", ".card"}:
        return parse_json_card(target.read_bytes())
    raise CharacterCardError("仅支持 JSON、CARD 或 PNG 角色卡")
