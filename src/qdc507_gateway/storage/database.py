from __future__ import annotations

import sqlite3
import threading
from dataclasses import asdict
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS sms_messages (
  id TEXT PRIMARY KEY,
  sender TEXT NOT NULL,
  body TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  is_read INTEGER NOT NULL DEFAULT 0,
  raw_pdus TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS sms_pdu_dedup (
  pdu_hash TEXT PRIMARY KEY,
  sender TEXT,
  concat_reference INTEGER,
  concat_sequence INTEGER,
  first_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS call_records (
  id TEXT PRIMARY KEY,
  direction TEXT NOT NULL,
  state TEXT NOT NULL,
  cellular_number TEXT,
  telegram_user_id INTEGER,
  frontend TEXT NOT NULL DEFAULT 'telegram',
  started_at TEXT NOT NULL,
  connected_at TEXT,
  ended_at TEXT,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS module_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_token (
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  token_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);
DROP TABLE IF EXISTS api_tokens;
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            database_path = Path(self.path)
            database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            database_path.parent.chmod(0o700)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.connection.executescript(SCHEMA)
        self.connection.commit()
        if self.path != ":memory:":
            try:
                Path(self.path).chmod(0o600)
            except OSError as exc:
                self.connection.close()
                raise PermissionError(
                    "SQLite database permissions could not be restricted to 0600"
                ) from exc

    def close(self) -> None:
        self.connection.close()

    def replace_token(self, token_hash: str, created_at: str) -> bool:
        """Replace the singleton API token and return whether one existed."""
        with self._lock:
            existed = self.connection.execute(
                "SELECT 1 FROM api_token WHERE singleton = 1"
            ).fetchone() is not None
            self.connection.execute(
                """
                INSERT INTO api_token(singleton, token_hash, created_at)
                VALUES (1, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                  token_hash = excluded.token_hash,
                  created_at = excluded.created_at
                """,
                (token_hash, created_at),
            )
            self.connection.commit()
            return existed

    def delete_token(self) -> bool:
        with self._lock:
            cursor = self.connection.execute(
                "DELETE FROM api_token WHERE singleton = 1"
            )
            self.connection.commit()
            return cursor.rowcount == 1

    def token(self) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.connection.execute(
                "SELECT token_hash, created_at FROM api_token WHERE singleton = 1"
            ).fetchone()

    def insert_event(self, event_type: str, payload: str, created_at: str) -> None:
        with self._lock:
            self.connection.execute(
                "INSERT INTO module_events(event_type, payload, created_at) VALUES (?, ?, ?)",
                (event_type, payload, created_at),
            )
            self.connection.commit()

    def save_sms(self, message: Dict[str, Any]) -> None:
        with self._lock:
            self.connection.execute(
                "INSERT OR REPLACE INTO sms_messages(id, sender, body, timestamp, is_read, raw_pdus) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    message["id"], message["sender"], message["body"], message["timestamp"],
                    int(bool(message.get("is_read", False))), message.get("raw_pdus", "[]"),
                ),
            )
            self.connection.commit()

    def record_sms_pdu(
        self,
        pdu_hash: str,
        first_seen_at: str,
        sender: Optional[str] = None,
        concat_reference: Optional[int] = None,
        concat_sequence: Optional[int] = None,
    ) -> bool:
        """Record a PDU hash and return False when it was already ingested."""
        with self._lock:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO sms_pdu_dedup(
                  pdu_hash, sender, concat_reference, concat_sequence, first_seen_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (pdu_hash, sender, concat_reference, concat_sequence, first_seen_at),
            )
            self.connection.commit()
            return cursor.rowcount == 1

    def list_sms(self, limit: int = 50, unread: Optional[bool] = None) -> List[sqlite3.Row]:
        query = "SELECT id, sender, body, timestamp, is_read FROM sms_messages"
        params: List[Any] = []
        if unread is not None:
            query += " WHERE is_read = ?"
            params.append(int(unread))
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self._lock:
            return list(self.connection.execute(query, params))

    def save_call(self, record: Any) -> None:
        values = asdict(record)

        def scalar(value: Any) -> Any:
            if isinstance(value, Enum):
                return value.value
            if hasattr(value, "isoformat"):
                return value.isoformat()
            return value

        with self._lock:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO call_records(
                  id, direction, state, cellular_number, telegram_user_id, frontend,
                  started_at, connected_at, ended_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(scalar(values[key]) for key in (
                    "id", "direction", "state", "cellular_number", "telegram_user_id", "frontend",
                    "started_at", "connected_at", "ended_at", "last_error",
                )),
            )
            self.connection.commit()

    def list_calls(self, limit: int = 50) -> List[sqlite3.Row]:
        with self._lock:
            return list(self.connection.execute(
                """
                SELECT id, direction, state, cellular_number, telegram_user_id,
                       frontend, started_at, connected_at, ended_at, last_error
                FROM call_records
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ))
