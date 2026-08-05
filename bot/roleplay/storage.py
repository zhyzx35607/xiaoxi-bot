"""SQLite persistence for roleplay conversations and structured memory."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable


def _now() -> int:
    return int(time.time())


def _slug(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "-", value.strip()).strip("-")
    return value[:80] or uuid.uuid4().hex[:12]


class RoleplayStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS characters (
          id TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          data_json TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS personas (
          id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, name TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '', is_default INTEGER NOT NULL DEFAULT 0,
          created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
          UNIQUE(owner_id, name)
        );
        CREATE TABLE IF NOT EXISTS chats (
          id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, character_id TEXT NOT NULL,
          persona_id TEXT, title TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'normal',
          archived INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          FOREIGN KEY(character_id) REFERENCES characters(id) ON DELETE CASCADE,
          FOREIGN KEY(persona_id) REFERENCES personas(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS owner_state (
          owner_id INTEGER PRIMARY KEY, active_chat_id TEXT, mode TEXT NOT NULL DEFAULT 'normal',
          mode_expires_at INTEGER NOT NULL DEFAULT 0,
          FOREIGN KEY(active_chat_id) REFERENCES chats(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, role TEXT NOT NULL,
          content TEXT NOT NULL, created_at INTEGER NOT NULL,
          FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS chat_summaries (
          id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
          content TEXT NOT NULL, through_message_id INTEGER NOT NULL DEFAULT 0,
          created_at INTEGER NOT NULL,
          FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS memories (
          id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
          character_id TEXT NOT NULL, memory_type TEXT NOT NULL,
          subject TEXT NOT NULL DEFAULT '', predicate TEXT NOT NULL DEFAULT '',
          object TEXT NOT NULL DEFAULT '', content TEXT NOT NULL,
          keywords TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL DEFAULT 1.0,
          source_message_id INTEGER, locked INTEGER NOT NULL DEFAULT 0,
          archived INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE,
          FOREIGN KEY(character_id) REFERENCES characters(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS worldbooks (
          id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, name TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL, UNIQUE(owner_id, name)
        );
        CREATE TABLE IF NOT EXISTS world_entries (
          id INTEGER PRIMARY KEY AUTOINCREMENT, worldbook_id TEXT NOT NULL,
          keywords TEXT NOT NULL, content TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 100,
          enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          FOREIGN KEY(worldbook_id) REFERENCES worldbooks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS chat_worldbooks (
          chat_id TEXT NOT NULL, worldbook_id TEXT NOT NULL,
          PRIMARY KEY(chat_id, worldbook_id),
          FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE,
          FOREIGN KEY(worldbook_id) REFERENCES worldbooks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS scene_states (
          chat_id TEXT PRIMARY KEY, scene_id TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
          current_situation TEXT NOT NULL DEFAULT '', scene_time TEXT NOT NULL DEFAULT '',
          location TEXT NOT NULL DEFAULT '', active_characters_json TEXT NOT NULL DEFAULT '[]',
          story_progress INTEGER NOT NULL DEFAULT 0, stable_memory_json TEXT NOT NULL DEFAULT '{}',
          volatile_memory_json TEXT NOT NULL DEFAULT '{}', updated_at INTEGER NOT NULL,
          FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS story_beats (
          id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, scene_id TEXT NOT NULL,
          content TEXT NOT NULL, created_at INTEGER NOT NULL,
          FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS audit_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL,
          chat_id TEXT, event_type TEXT NOT NULL, detail_json TEXT NOT NULL,
          created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chats_owner_updated ON chats(owner_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_memories_chat_type ON memories(chat_id, memory_type, archived);
        CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC);
        """
        with self._lock, self._connect() as conn:
            conn.executescript(schema)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def import_character(self, card: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        slug = _slug(str(card["name"]))
        data_json = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as conn:
            existing = conn.execute("SELECT id FROM characters WHERE slug=?", (slug,)).fetchone()
            character_id = existing["id"] if existing else uuid.uuid4().hex
            conn.execute(
                "INSERT INTO characters(id,slug,name,data_json,created_at,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(slug) DO UPDATE SET name=excluded.name,data_json=excluded.data_json,updated_at=excluded.updated_at",
                (character_id, slug, card["name"], data_json, now, now),
            )
        return self.get_character(character_id)

    def list_characters(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id,slug,name,created_at,updated_at FROM characters ORDER BY name").fetchall()
        return [dict(row) for row in rows]

    def get_character(self, value: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM characters WHERE id=? OR slug=? OR name=? LIMIT 1", (value, value, value)
            ).fetchone()
        result = self._row(row)
        if result:
            result["data"] = json.loads(result.pop("data_json"))
        return result

    def delete_character(self, value: str) -> bool:
        character = self.get_character(value)
        if not character:
            return False
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM characters WHERE id=?", (character["id"],))
        return True

    def create_persona(self, owner_id: int, name: str, description: str, *, default: bool = False) -> dict[str, Any]:
        now = _now()
        persona_id = uuid.uuid4().hex
        with self._lock, self._connect() as conn:
            if default:
                conn.execute("UPDATE personas SET is_default=0 WHERE owner_id=?", (owner_id,))
            conn.execute(
                "INSERT INTO personas(id,owner_id,name,description,is_default,created_at,updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(owner_id,name) DO UPDATE SET description=excluded.description,is_default=excluded.is_default,updated_at=excluded.updated_at",
                (persona_id, owner_id, name[:120], description[:12000], int(default), now, now),
            )
            row = conn.execute("SELECT * FROM personas WHERE owner_id=? AND name=?", (owner_id, name[:120])).fetchone()
        return dict(row)

    def list_personas(self, owner_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM personas WHERE owner_id=? ORDER BY is_default DESC,name", (owner_id,)).fetchall()
        return [dict(row) for row in rows]

    def get_persona(self, owner_id: int, value: str | None) -> dict[str, Any] | None:
        with self._connect() as conn:
            if value:
                row = conn.execute("SELECT * FROM personas WHERE owner_id=? AND (id=? OR name=?) LIMIT 1", (owner_id, value, value)).fetchone()
            else:
                row = conn.execute("SELECT * FROM personas WHERE owner_id=? ORDER BY is_default DESC,created_at LIMIT 1", (owner_id,)).fetchone()
        return self._row(row)

    def set_active_chat_persona(self, owner_id: int, value: str) -> dict[str, Any] | None:
        active = self.active_chat(owner_id)
        if not active:
            return None
        persona = self.get_persona(owner_id, value)
        if not persona:
            return None
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE chats SET persona_id=?,updated_at=? WHERE id=?", (persona["id"], _now(), active["id"]))
        return persona

    def delete_persona(self, owner_id: int, value: str) -> bool:
        persona = self.get_persona(owner_id, value)
        if not persona:
            return False
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM personas WHERE id=? AND owner_id=?", (persona["id"], owner_id))
            return cursor.rowcount > 0

    def new_chat(self, owner_id: int, character_id: str, *, persona_id: str | None = None, title: str = "") -> dict[str, Any]:
        now = _now()
        chat_id = uuid.uuid4().hex
        character = self.get_character(character_id)
        if not character:
            raise ValueError("角色不存在")
        title = (title.strip() or f"{character['name']}-{time.strftime('%Y%m%d-%H%M')}")[:160]
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO chats(id,owner_id,character_id,persona_id,title,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (chat_id, owner_id, character["id"], persona_id, title, now, now),
            )
            conn.execute(
                "INSERT INTO owner_state(owner_id,active_chat_id) VALUES(?,?) "
                "ON CONFLICT(owner_id) DO UPDATE SET active_chat_id=excluded.active_chat_id",
                (owner_id, chat_id),
            )
        return self.get_chat(chat_id)

    def get_chat(self, chat_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT c.*,ch.name character_name,p.name persona_name,p.description persona_description "
                "FROM chats c JOIN characters ch ON ch.id=c.character_id "
                "LEFT JOIN personas p ON p.id=c.persona_id WHERE c.id=?", (chat_id,)
            ).fetchone()
        return self._row(row)

    def active_chat(self, owner_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT active_chat_id FROM owner_state WHERE owner_id=?", (owner_id,)).fetchone()
        return self.get_chat(row["active_chat_id"]) if row and row["active_chat_id"] else None

    def list_chats(self, owner_id: int, limit: int = 30) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT c.*,ch.name character_name FROM chats c JOIN characters ch ON ch.id=c.character_id "
                "WHERE c.owner_id=? AND c.archived=0 ORDER BY c.updated_at DESC LIMIT ?", (owner_id, limit)
            ).fetchall()
        return [dict(row) for row in rows]

    def use_chat(self, owner_id: int, value: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM chats WHERE owner_id=? AND (id=? OR title=?) AND archived=0 LIMIT 1", (owner_id, value, value)
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "INSERT INTO owner_state(owner_id,active_chat_id) VALUES(?,?) "
                "ON CONFLICT(owner_id) DO UPDATE SET active_chat_id=excluded.active_chat_id", (owner_id, row["id"])
            )
        return self.get_chat(row["id"])

    def rename_chat(self, owner_id: int, title: str) -> bool:
        active = self.active_chat(owner_id)
        if not active:
            return False
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE chats SET title=?,updated_at=? WHERE id=?", (title[:160], _now(), active["id"]))
        return True

    def archive_chat(self, owner_id: int) -> bool:
        active = self.active_chat(owner_id)
        if not active:
            return False
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE chats SET archived=1,updated_at=? WHERE id=?", (_now(), active["id"]))
            conn.execute("UPDATE owner_state SET active_chat_id=NULL WHERE owner_id=?", (owner_id,))
        return True

    def set_mode(self, owner_id: int, mode: str, ttl_seconds: int = 1800) -> None:
        expires = _now() + ttl_seconds if mode != "normal" else 0
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO owner_state(owner_id,mode,mode_expires_at) VALUES(?,?,?) "
                "ON CONFLICT(owner_id) DO UPDATE SET mode=excluded.mode,mode_expires_at=excluded.mode_expires_at",
                (owner_id, mode, expires),
            )

    def get_mode(self, owner_id: int) -> tuple[str, int]:
        with self._connect() as conn:
            row = conn.execute("SELECT mode,mode_expires_at FROM owner_state WHERE owner_id=?", (owner_id,)).fetchone()
        if not row:
            return "normal", 0
        if row["mode_expires_at"] and row["mode_expires_at"] <= _now():
            self.set_mode(owner_id, "normal")
            return "normal", 0
        return str(row["mode"]), int(row["mode_expires_at"])

    def add_message(self, chat_id: str, role: str, content: str) -> int:
        if role not in {"user", "assistant", "system"}:
            raise ValueError("invalid message role")
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO messages(chat_id,role,content,created_at) VALUES(?,?,?,?)",
                (chat_id, role, content[:50000], _now()),
            )
            conn.execute("UPDATE chats SET updated_at=? WHERE id=?", (_now(), chat_id))
            return int(cursor.lastrowid)

    def recent_messages(self, chat_id: str, limit: int = 24) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def message_count(self, chat_id: str) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id=?", (chat_id,)).fetchone()[0])

    def save_summary(self, chat_id: str, content: str, through_message_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("INSERT INTO chat_summaries(chat_id,content,through_message_id,created_at) VALUES(?,?,?,?)", (chat_id, content[:12000], through_message_id, _now()))

    def latest_summary(self, chat_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM chat_summaries WHERE chat_id=? ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
        return self._row(row)

    def add_memory(self, chat_id: str, memory_type: str, content: str, *, subject: str = "", predicate: str = "", object_value: str = "", keywords: str = "", confidence: float = 1.0, source_message_id: int | None = None, locked: bool = False) -> int:
        chat = self.get_chat(chat_id)
        if not chat:
            raise ValueError("聊天不存在")
        now = _now()
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO memories(chat_id,character_id,memory_type,subject,predicate,object,content,keywords,confidence,source_message_id,locked,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (chat_id, chat["character_id"], memory_type[:80], subject[:200], predicate[:200], object_value[:500], content[:8000], keywords[:1000], max(0.0, min(1.0, confidence)), source_message_id, int(locked), now, now),
            )
            return int(cursor.lastrowid)

    def list_memories(self, chat_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM memories WHERE chat_id=? AND archived=0 ORDER BY locked DESC,updated_at DESC LIMIT ?", (chat_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def search_memories(self, chat_id: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
        raw_parts = [part for part in re.split(r"\s+|[,，。！？;；]+", query) if len(part) >= 2]
        tokens: list[str] = []
        for part in raw_parts:
            tokens.append(part)
            if re.search(r"[\u4e00-\u9fff]", part) and len(part) > 2:
                for size in (4, 3, 2):
                    for index in range(0, len(part) - size + 1):
                        tokens.append(part[index:index + size])
            if len(tokens) >= 24:
                break
        tokens = list(dict.fromkeys(tokens))[:24]
        if not tokens:
            return self.list_memories(chat_id, limit)
        clauses = []
        params: list[Any] = [chat_id]
        for token in tokens:
            clauses.append("(content LIKE ? OR keywords LIKE ? OR subject LIKE ? OR object LIKE ?)")
            pattern = f"%{token}%"
            params.extend([pattern, pattern, pattern, pattern])
        params.append(limit)
        sql = "SELECT * FROM memories WHERE chat_id=? AND archived=0 AND (" + " OR ".join(clauses) + ") ORDER BY locked DESC,confidence DESC,updated_at DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def update_memory(self, chat_id: str, memory_id: int, content: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE memories SET content=?,keywords=?,updated_at=? "
                "WHERE id=? AND chat_id=? AND archived=0",
                (content[:8000], content[:1000], _now(), memory_id, chat_id),
            )
            return cursor.rowcount > 0

    def set_memory_state(self, chat_id: str, memory_id: int, *, locked: bool | None = None, archived: bool | None = None) -> bool:
        updates, params = [], []
        if locked is not None:
            updates.append("locked=?"); params.append(int(locked))
        if archived is not None:
            updates.append("archived=?"); params.append(int(archived))
        if not updates:
            return False
        updates.append("updated_at=?"); params.extend([_now(), memory_id, chat_id])
        with self._lock, self._connect() as conn:
            cursor = conn.execute("UPDATE memories SET " + ",".join(updates) + " WHERE id=? AND chat_id=?", params)
            return cursor.rowcount > 0

    def create_worldbook(self, owner_id: int, name: str) -> dict[str, Any]:
        now = _now(); worldbook_id = uuid.uuid4().hex
        with self._lock, self._connect() as conn:
            conn.execute("INSERT INTO worldbooks(id,owner_id,name,created_at,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(owner_id,name) DO UPDATE SET updated_at=excluded.updated_at", (worldbook_id, owner_id, name[:120], now, now))
            row = conn.execute("SELECT * FROM worldbooks WHERE owner_id=? AND name=?", (owner_id, name[:120])).fetchone()
        return dict(row)

    def list_worldbooks(self, owner_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM worldbooks WHERE owner_id=? ORDER BY name", (owner_id,)).fetchall()
        return [dict(row) for row in rows]

    def get_worldbook(self, owner_id: int, value: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            book = conn.execute("SELECT * FROM worldbooks WHERE owner_id=? AND (id=? OR name=?) LIMIT 1", (owner_id, value, value)).fetchone()
            if not book:
                return None
            entries = conn.execute("SELECT * FROM world_entries WHERE worldbook_id=? ORDER BY priority DESC,id", (book["id"],)).fetchall()
        result = dict(book)
        result["entries"] = [dict(row) for row in entries]
        return result

    def delete_worldbook(self, owner_id: int, value: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM worldbooks WHERE owner_id=? AND (id=? OR name=?)", (owner_id, value, value))
            return cursor.rowcount > 0

    def add_world_entry(self, owner_id: int, worldbook: str, keywords: str, content: str, priority: int = 100) -> int:
        book = self.create_worldbook(owner_id, worldbook)
        now = _now()
        with self._lock, self._connect() as conn:
            cursor = conn.execute("INSERT INTO world_entries(worldbook_id,keywords,content,priority,created_at,updated_at) VALUES(?,?,?,?,?,?)", (book["id"], keywords[:1000], content[:12000], priority, now, now))
            return int(cursor.lastrowid)

    def bind_worldbook(self, chat_id: str, owner_id: int, worldbook: str) -> bool:
        with self._lock, self._connect() as conn:
            book = conn.execute("SELECT id FROM worldbooks WHERE owner_id=? AND (id=? OR name=?) LIMIT 1", (owner_id, worldbook, worldbook)).fetchone()
            if not book:
                return False
            conn.execute("INSERT OR IGNORE INTO chat_worldbooks(chat_id,worldbook_id) VALUES(?,?)", (chat_id, book["id"]))
            return True

    def matching_world_entries(self, chat_id: str, text: str, limit: int = 8) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT e.* FROM world_entries e JOIN chat_worldbooks cw ON cw.worldbook_id=e.worldbook_id WHERE cw.chat_id=? AND e.enabled=1 ORDER BY e.priority DESC", (chat_id,)).fetchall()
        matches = []
        lowered = text.lower()
        for row in rows:
            keywords = [k.strip().lower() for k in re.split(r"[,，|]", row["keywords"]) if k.strip()]
            if not keywords or any(keyword in lowered for keyword in keywords):
                matches.append(dict(row))
            if len(matches) >= limit:
                break
        return matches

    def get_scene_state(self, chat_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM scene_states WHERE chat_id=?", (chat_id,)).fetchone()
        result = self._row(row)
        if result:
            result["active_characters"] = json.loads(result.pop("active_characters_json") or "[]")
            result["stable_memory"] = json.loads(result.pop("stable_memory_json") or "{}")
            result["volatile_memory"] = json.loads(result.pop("volatile_memory_json") or "{}")
        return result

    def update_scene_state(self, chat_id: str, **fields: Any) -> dict[str, Any]:
        current = self.get_scene_state(chat_id) or {
            "scene_id": uuid.uuid4().hex, "title": "", "current_situation": "",
            "scene_time": "", "location": "", "active_characters": [],
            "story_progress": 0, "stable_memory": {}, "volatile_memory": {},
        }
        for key in ("title", "current_situation", "scene_time", "location", "story_progress", "active_characters"):
            if key in fields and fields[key] is not None:
                current[key] = fields[key]
        if fields.get("change_scene"):
            current["scene_id"] = uuid.uuid4().hex
            current["volatile_memory"] = {}
        for memory_key in ("stable_memory", "volatile_memory"):
            patch = fields.get(memory_key)
            if isinstance(patch, dict):
                base = current.get(memory_key) if isinstance(current.get(memory_key), dict) else {}
                current[memory_key] = {**base, **patch}
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO scene_states(chat_id,scene_id,title,current_situation,scene_time,location,active_characters_json,story_progress,stable_memory_json,volatile_memory_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET scene_id=excluded.scene_id,title=excluded.title,current_situation=excluded.current_situation,scene_time=excluded.scene_time,location=excluded.location,active_characters_json=excluded.active_characters_json,story_progress=excluded.story_progress,stable_memory_json=excluded.stable_memory_json,volatile_memory_json=excluded.volatile_memory_json,updated_at=excluded.updated_at",
                (chat_id, current["scene_id"], str(current["title"])[:300], str(current["current_situation"])[:4000], str(current["scene_time"])[:200], str(current["location"])[:300], json.dumps(current["active_characters"], ensure_ascii=False), max(0, min(100, int(current["story_progress"] or 0))), json.dumps(current["stable_memory"], ensure_ascii=False), json.dumps(current["volatile_memory"], ensure_ascii=False), _now()),
            )
        return self.get_scene_state(chat_id)

    def add_story_beat(self, chat_id: str, content: str) -> int:
        scene = self.get_scene_state(chat_id) or self.update_scene_state(chat_id)
        with self._lock, self._connect() as conn:
            cursor = conn.execute("INSERT INTO story_beats(chat_id,scene_id,content,created_at) VALUES(?,?,?,?)", (chat_id, scene["scene_id"], content[:8000], _now()))
            return int(cursor.lastrowid)

    def recent_story_beats(self, chat_id: str, limit: int = 12) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM story_beats WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def relationship_timeline(self, chat_id: str, limit: int = 30) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM memories WHERE chat_id=? AND memory_type IN ('relationship_state','relationship_event') AND archived=0 ORDER BY created_at DESC LIMIT ?", (chat_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def audit(self, owner_id: int, event_type: str, detail: dict[str, Any], chat_id: str | None = None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("INSERT INTO audit_events(owner_id,chat_id,event_type,detail_json,created_at) VALUES(?,?,?,?,?)", (owner_id, chat_id, event_type, json.dumps(detail, ensure_ascii=False, separators=(",", ":")), _now()))

    def export_chat(self, chat_id: str) -> dict[str, Any]:
        chat = self.get_chat(chat_id)
        if not chat:
            raise ValueError("聊天不存在")
        return {
            "format": "qqbot-roleplay-v1",
            "chat": chat,
            "messages": self.recent_messages(chat_id, 100000),
            "summary": self.latest_summary(chat_id),
            "memories": self.list_memories(chat_id, 100000),
        }
