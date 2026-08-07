"""Durable owner-companion state, memory, events, follow-ups and outbox."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def _now() -> float:
    return time.time()


class CompanionStore:
    """SQLite store kept separate from roleplay and ordinary chat memory."""

    def __init__(self, root: str | Path):
        self.path = Path(root) / "companion.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self):
        schema = """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS state (
          owner_id INTEGER PRIMARY KEY, data_json TEXT NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS facts (
          id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, category TEXT NOT NULL,
          fact_key TEXT NOT NULL, content TEXT NOT NULL, value_json TEXT NOT NULL,
          source TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL DEFAULT 0.8,
          active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, updated_at REAL NOT NULL,
          UNIQUE(owner_id, category, fact_key)
        );
        CREATE TABLE IF NOT EXISTS temporal_events (
          id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, kind TEXT NOT NULL,
          title TEXT NOT NULL, due_month INTEGER, due_day INTEGER, due_at REAL,
          recurrence TEXT NOT NULL DEFAULT '', last_trigger_key TEXT NOT NULL DEFAULT '',
          payload_json TEXT NOT NULL DEFAULT '{}', active INTEGER NOT NULL DEFAULT 1,
          created_at REAL NOT NULL, updated_at REAL NOT NULL,
          UNIQUE(owner_id, kind, title)
        );
        CREATE TABLE IF NOT EXISTS episodes (
          id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, content TEXT NOT NULL,
          tags_json TEXT NOT NULL DEFAULT '[]', source TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS relationship_events (
          id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, kind TEXT NOT NULL,
          content TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS emotion_snapshots (
          id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, data_json TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS followups (
          id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, topic TEXT NOT NULL,
          payload_json TEXT NOT NULL, next_at REAL NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
          max_attempts INTEGER NOT NULL DEFAULT 4, cooldown_until REAL NOT NULL DEFAULT 0,
          active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_due ON temporal_events(owner_id, active, due_month, due_day);
        CREATE INDEX IF NOT EXISTS idx_followups_due ON followups(owner_id, active, next_at);
        CREATE TABLE IF NOT EXISTS outbox (
          id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, topic TEXT NOT NULL,
          payload_json TEXT NOT NULL, due_at REAL NOT NULL, priority TEXT NOT NULL DEFAULT 'normal',
          idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
          created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(owner_id, status, due_at, priority);
        """
        with self._lock, self._connection() as conn:
            conn.executescript(schema)

    def get_state(self, owner_id: int, default: dict) -> dict:
        with self._connection() as conn:
            row = conn.execute("SELECT data_json FROM state WHERE owner_id=?", (int(owner_id),)).fetchone()
        if not row:
            return dict(default)
        try:
            value = json.loads(row["data_json"])
            return value if isinstance(value, dict) else dict(default)
        except (TypeError, ValueError, json.JSONDecodeError):
            return dict(default)

    def save_state(self, owner_id: int, value: dict):
        now = _now()
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT INTO state(owner_id,data_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(owner_id) DO UPDATE SET data_json=excluded.data_json,updated_at=excluded.updated_at",
                (int(owner_id), json.dumps(value, ensure_ascii=False), now),
            )

    def upsert_fact(self, owner_id: int, category: str, fact_key: str, content: str,
                    value=None, source: str = "", confidence: float = 0.8):
        now = _now()
        item_id = uuid.uuid4().hex
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT INTO facts(id,owner_id,category,fact_key,content,value_json,source,confidence,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(owner_id,category,fact_key) DO UPDATE SET content=excluded.content,"
                "value_json=excluded.value_json,source=excluded.source,confidence=excluded.confidence,"
                "active=1,updated_at=excluded.updated_at",
                (item_id, int(owner_id), str(category)[:80], str(fact_key)[:160], str(content)[:2000],
                 json.dumps(value if value is not None else content, ensure_ascii=False), str(source)[:200],
                 max(0.0, min(1.0, float(confidence))), now, now),
            )

    def list_facts(self, owner_id: int, query: str = "", limit: int = 100):
        query = str(query or "").strip()
        with self._connection() as conn:
            if query:
                pattern = "%{}%".format(query)
                rows = conn.execute(
                    "SELECT * FROM facts WHERE owner_id=? AND active=1 AND "
                    "(category LIKE ? OR fact_key LIKE ? OR content LIKE ?) "
                    "ORDER BY updated_at DESC LIMIT ?", (int(owner_id), pattern, pattern, pattern, int(limit)))
            else:
                rows = conn.execute(
                    "SELECT * FROM facts WHERE owner_id=? AND active=1 ORDER BY updated_at DESC LIMIT ?",
                    (int(owner_id), int(limit)))
            return [dict(row) for row in rows]

    def archive_fact(self, owner_id: int, fact_id: str):
        with self._lock, self._connection() as conn:
            cur = conn.execute("UPDATE facts SET active=0,updated_at=? WHERE owner_id=? AND id=?",
                               (_now(), int(owner_id), str(fact_id)))
            return cur.rowcount > 0

    def upsert_event(self, owner_id: int, kind: str, title: str, month: int, day: int,
                     recurrence: str = "yearly", payload=None):
        now = _now()
        event_id = uuid.uuid4().hex
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT INTO temporal_events(id,owner_id,kind,title,due_month,due_day,recurrence,payload_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(owner_id,kind,title) DO UPDATE SET due_month=excluded.due_month,"
                "due_day=excluded.due_day,recurrence=excluded.recurrence,payload_json=excluded.payload_json,active=1,updated_at=excluded.updated_at",
                (event_id, int(owner_id), str(kind)[:80], str(title)[:240], int(month), int(day), str(recurrence)[:30],
                 json.dumps(payload or {}, ensure_ascii=False), now, now),
            )

    def due_events(self, owner_id: int, month: int, day: int):
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM temporal_events WHERE owner_id=? AND active=1 AND due_month=? AND due_day=?",
                (int(owner_id), int(month), int(day))).fetchall()
            return [dict(row) for row in rows]

    def mark_event_triggered(self, event_id: str, trigger_key: str):
        with self._lock, self._connection() as conn:
            conn.execute("UPDATE temporal_events SET last_trigger_key=?,updated_at=? WHERE id=?",
                         (str(trigger_key)[:80], _now(), str(event_id)))

    def add_episode(self, owner_id: int, content: str, tags=None, source: str = ""):
        with self._lock, self._connection() as conn:
            conn.execute("INSERT INTO episodes(id,owner_id,content,tags_json,source,created_at) VALUES(?,?,?,?,?,?)",
                         (uuid.uuid4().hex, int(owner_id), str(content)[:8000], json.dumps(tags or [], ensure_ascii=False),
                          str(source)[:200], _now()))

    def search_episodes(self, owner_id: int, query: str = "", limit: int = 20):
        with self._connection() as conn:
            if query:
                rows = conn.execute("SELECT * FROM episodes WHERE owner_id=? AND content LIKE ? ORDER BY created_at DESC LIMIT ?",
                                    (int(owner_id), "%{}%".format(query), int(limit))).fetchall()
            else:
                rows = conn.execute("SELECT * FROM episodes WHERE owner_id=? ORDER BY created_at DESC LIMIT ?",
                                    (int(owner_id), int(limit))).fetchall()
            return [dict(row) for row in rows]

    def add_relationship_event(self, owner_id: int, kind: str, content: str, payload=None):
        with self._lock, self._connection() as conn:
            conn.execute("INSERT INTO relationship_events(id,owner_id,kind,content,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                         (uuid.uuid4().hex, int(owner_id), str(kind)[:80], str(content)[:4000],
                          json.dumps(payload or {}, ensure_ascii=False), _now()))

    def add_emotion_snapshot(self, owner_id: int, state: dict):
        with self._lock, self._connection() as conn:
            conn.execute("INSERT INTO emotion_snapshots(id,owner_id,data_json,created_at) VALUES(?,?,?,?)",
                         (uuid.uuid4().hex, int(owner_id), json.dumps(state, ensure_ascii=False), _now()))

    def add_followup(self, owner_id: int, topic: str, payload: dict, next_at: float,
                     attempt: int = 0, max_attempts: int = 4):
        with self._lock, self._connection() as conn:
            conn.execute("INSERT INTO followups(id,owner_id,topic,payload_json,next_at,attempt,max_attempts,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (uuid.uuid4().hex, int(owner_id), str(topic)[:160], json.dumps(payload, ensure_ascii=False),
                          float(next_at), int(attempt), int(max_attempts), _now(), _now()))

    def due_followups(self, owner_id: int, now: float):
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM followups WHERE owner_id=? AND active=1 AND next_at<=? ORDER BY next_at LIMIT 20",
                                (int(owner_id), float(now))).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                try:
                    item["payload"] = json.loads(item.pop("payload_json"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    item["payload"] = {}
                result.append(item)
            return result

    def finish_followup(self, followup_id: str, cooldown_until: float = 0):
        with self._lock, self._connection() as conn:
            conn.execute("UPDATE followups SET active=0,cooldown_until=?,updated_at=? WHERE id=?",
                         (float(cooldown_until), _now(), str(followup_id)))

    def cancel_followups(self, owner_id: int, topic: str = ""):
        with self._lock, self._connection() as conn:
            if topic:
                conn.execute("UPDATE followups SET active=0,updated_at=? WHERE owner_id=? AND topic=? AND active=1",
                             (_now(), int(owner_id), str(topic)))
            else:
                conn.execute("UPDATE followups SET active=0,updated_at=? WHERE owner_id=? AND active=1",
                             (_now(), int(owner_id)))

    def enqueue(self, owner_id: int, topic: str, payload: dict, due_at: float,
                priority: str, idempotency_key: str):
        item_id = uuid.uuid4().hex
        now = _now()
        with self._lock, self._connection() as conn:
            conn.execute("INSERT OR IGNORE INTO outbox(id,owner_id,topic,payload_json,due_at,priority,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (item_id, int(owner_id), str(topic)[:160], json.dumps(payload, ensure_ascii=False), float(due_at),
                          str(priority)[:20], str(idempotency_key)[:200], now, now))

    def due_outbox(self, owner_id: int, now: float, limit: int = 20):
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM outbox WHERE owner_id=? AND status='pending' AND due_at<=? ORDER BY CASE priority WHEN 'urgent' THEN 0 ELSE 1 END,due_at LIMIT ?",
                                (int(owner_id), float(now), int(limit))).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                try:
                    item["payload"] = json.loads(item.pop("payload_json"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    item["payload"] = {}
                result.append(item)
            return result

    def mark_outbox(self, item_id: str, status: str, error: str = "", due_at: float | None = None):
        with self._lock, self._connection() as conn:
            if due_at is None:
                conn.execute("UPDATE outbox SET status=?,attempts=attempts+1,last_error=?,updated_at=? WHERE id=?",
                             (str(status)[:30], str(error)[:500], _now(), str(item_id)))
            else:
                conn.execute("UPDATE outbox SET status=?,attempts=attempts+1,last_error=?,due_at=?,updated_at=? WHERE id=?",
                             (str(status)[:30], str(error)[:500], float(due_at), _now(), str(item_id)))
