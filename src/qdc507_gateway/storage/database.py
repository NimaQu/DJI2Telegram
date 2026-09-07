from __future__ import annotations

import sqlite3
import json
import threading
import time
import uuid
from dataclasses import asdict
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  name TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS push_devices (
  installation_id TEXT PRIMARY KEY,
  device_token TEXT NOT NULL,
  environment TEXT NOT NULL,
  bundle_id TEXT NOT NULL,
  version TEXT NOT NULL,
  registered_at REAL NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  UNIQUE(device_token, environment, bundle_id)
);
CREATE TABLE IF NOT EXISTS push_jobs (
  id TEXT PRIMARY KEY,
  sms_id TEXT NOT NULL,
  installation_id TEXT NOT NULL,
  version TEXT NOT NULL,
  created_at REAL NOT NULL,
  next_attempt REAL NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  UNIQUE(sms_id, installation_id, version)
);
CREATE INDEX IF NOT EXISTS push_jobs_due ON push_jobs(next_attempt);
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
        self.push_scope: tuple[str, str] | None = None
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

        self._migrate_sms_utc()

    def _migrate_sms_utc(self) -> None:
        """Repair the former local-time-as-UTC bug from preserved source PDUs once.

        Updates timestamps only: never reingest PDUs or enqueue notifications.
        Rows without a valid source timestamp retain their existing fallback time.
        """
        from qdc507_gateway.modem.sms import SMSPDUError, decode_deliver

        name = "sms_scts_utc_v1"
        with self._lock, self.connection:
            if self.connection.execute("SELECT 1 FROM schema_migrations WHERE name=?", (name,)).fetchone():
                return
            for row in self.connection.execute("SELECT id,raw_pdus FROM sms_messages").fetchall():
                try:
                    pdus = json.loads(row["raw_pdus"])
                except (ValueError, TypeError):
                    continue
                if not isinstance(pdus, list):
                    continue
                for pdu in pdus:
                    if not isinstance(pdu, str):
                        continue
                    try:
                        timestamp = decode_deliver(pdu).timestamp
                    except (SMSPDUError, ValueError):
                        continue
                    if timestamp is not None:
                        self.connection.execute("UPDATE sms_messages SET timestamp=? WHERE id=?",
                                                (timestamp.isoformat(), row["id"]))
                        break
            self.connection.execute("INSERT INTO schema_migrations(name) VALUES (?)", (name,))

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

    def save_sms(self, message: Dict[str, Any], *, inbound: bool = False) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO sms_messages(id, sender, body, timestamp, is_read, raw_pdus) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    message["id"], message["sender"], message["body"], message["timestamp"],
                    int(bool(message.get("is_read", False))), message.get("raw_pdus", "[]"),
                ),
            )
            if inbound and self.push_scope is not None:
                now = time.time()
                for device in self.connection.execute(
                    "SELECT installation_id, version FROM push_devices WHERE active=1 AND environment=? AND bundle_id=?",
                    self.push_scope,
                ).fetchall():
                    self.connection.execute(
                        "INSERT OR IGNORE INTO push_jobs(id,sms_id,installation_id,version,created_at,next_attempt) VALUES (?,?,?,?,?,?)",
                        (str(uuid.uuid4()), message["id"], device["installation_id"], device["version"], now, now),
                    )

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

    def get_sms(self, sms_id: str):
        with self._lock:
            return self.connection.execute(
                "SELECT id,sender,body,timestamp,is_read FROM sms_messages WHERE id=?", (sms_id,)
            ).fetchone()

    def register_push_device(self, installation_id: str, device_token: str,
                             environment: str, bundle_id: str) -> None:
        with self._lock, self.connection:
            old = self.connection.execute(
                "SELECT * FROM push_devices WHERE installation_id=?", (installation_id,)
            ).fetchone()
            # A token has one owner in a topic/environment, even across reinstallations.
            duplicates = self.connection.execute(
                "SELECT installation_id FROM push_devices WHERE device_token=? AND environment=? AND bundle_id=? AND installation_id<>?",
                (device_token, environment, bundle_id, installation_id),
            ).fetchall()
            for row in duplicates:
                self.connection.execute("DELETE FROM push_jobs WHERE installation_id=?", (row[0],))
                self.connection.execute("DELETE FROM push_devices WHERE installation_id=?", (row[0],))
            same = old is not None and old["active"] and (
                old["device_token"], old["environment"], old["bundle_id"]
            ) == (device_token, environment, bundle_id)
            if same:
                # Refresh timestamp so an older 410 response cannot revoke a fresh registration.
                self.connection.execute("UPDATE push_devices SET registered_at=? WHERE installation_id=?",
                                        (time.time(), installation_id))
                return
            self.connection.execute("DELETE FROM push_jobs WHERE installation_id=?", (installation_id,))
            self.connection.execute(
                "INSERT OR REPLACE INTO push_devices VALUES (?,?,?,?,?,?,1)",
                (installation_id, device_token, environment, bundle_id, str(uuid.uuid4()), time.time()),
            )

    def delete_push_device(self, installation_id: str) -> None:
        with self._lock, self.connection:
            self.connection.execute("DELETE FROM push_jobs WHERE installation_id=?", (installation_id,))
            self.connection.execute("DELETE FROM push_devices WHERE installation_id=?", (installation_id,))

    def next_push_job(self, environment: str, bundle_id: str, now: float):
        with self._lock, self.connection:
            self.connection.execute("DELETE FROM push_jobs WHERE created_at<=?", (now - 86400,))
            # Changing scope must never replay an old environment's queue later.
            self.connection.execute(
                "DELETE FROM push_jobs WHERE NOT EXISTS (SELECT 1 FROM push_devices d WHERE d.installation_id=push_jobs.installation_id AND d.version=push_jobs.version AND d.active=1 AND d.environment=? AND d.bundle_id=?)",
                (environment, bundle_id),
            )
            return self.connection.execute(
                """SELECT j.*, d.device_token, d.registered_at, s.sender,s.body,s.timestamp
                FROM push_jobs j JOIN push_devices d ON d.installation_id=j.installation_id AND d.version=j.version
                JOIN sms_messages s ON s.id=j.sms_id
                WHERE j.next_attempt<=? ORDER BY j.next_attempt LIMIT 1""", (now,)
            ).fetchone()

    def finish_push_job(self, job_id: str) -> None:
        with self._lock, self.connection:
            self.connection.execute("DELETE FROM push_jobs WHERE id=?", (job_id,))

    def retry_push_job(self, job_id: str, next_attempt: float) -> None:
        with self._lock, self.connection:
            self.connection.execute("UPDATE push_jobs SET attempts=attempts+1,next_attempt=? WHERE id=?",
                                    (next_attempt, job_id))

    def invalidate_push_device(self, job, invalidated_at: float | None = None) -> None:
        cutoff = job["registered_at"] if invalidated_at is None else invalidated_at
        with self._lock, self.connection:
            changed = self.connection.execute(
                "UPDATE push_devices SET active=0 WHERE installation_id=? AND version=? AND registered_at<=?",
                (job["installation_id"], job["version"], cutoff),
            ).rowcount
            if changed:
                self.connection.execute("DELETE FROM push_jobs WHERE installation_id=? AND version=?",
                                        (job["installation_id"], job["version"]))
            self.connection.execute("DELETE FROM push_jobs WHERE id=?", (job["id"],))

    def push_counts(self, environment: str, bundle_id: str):
        with self._lock:
            devices = self.connection.execute(
                "SELECT COUNT(*) FROM push_devices WHERE active=1 AND environment=? AND bundle_id=?",
                (environment, bundle_id),
            ).fetchone()[0]
            jobs = self.connection.execute("SELECT COUNT(*) FROM push_jobs").fetchone()[0]
            return {"active_devices": devices, "queued": jobs}


    def reconcile_push_scope(self, environment: str, bundle_id: str) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE push_devices SET active=0 WHERE environment<>? OR bundle_id<>?",
                (environment, bundle_id),
            )
            self.connection.execute(
                "DELETE FROM push_jobs WHERE created_at<=? OR NOT EXISTS (SELECT 1 FROM push_devices d WHERE d.installation_id=push_jobs.installation_id AND d.version=push_jobs.version AND d.active=1)",
                (time.time() - 86400,),
            )
