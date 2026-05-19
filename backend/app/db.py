"""SQLite-backed stream configuration store.

Each record is a named stream slot with its full config. Configs persist
independently of whether the stream is currently running — the runtime
state lives only in memory (StreamRegistry in api.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_DB_PATH = Path(os.environ.get("SCTE35_DATA_DIR", "/opt/scte35/data")) / "streams.db"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS streams (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL UNIQUE,
            data       TEXT    NOT NULL,
            created_at REAL    NOT NULL,
            updated_at REAL    NOT NULL
        );
    """)
    conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id":         row["id"],
        "name":       row["name"],
        "config":     json.loads(row["data"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class StreamStore:
    """Thread-safe, async-friendly SQLite store for stream configurations."""

    def __init__(self, db_path: Path = _DB_PATH):
        self._path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def _open(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = _connect(self._path)
            _init_schema(self._conn)
        return self._conn

    # ── Sync internals ────────────────────────────────────────────────────────

    def _sync_list(self) -> list[dict]:
        conn = self._open()
        rows = conn.execute(
            "SELECT id, name, data, created_at, updated_at FROM streams ORDER BY updated_at DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def _sync_create(self, name: str, data: dict) -> dict:
        conn = self._open()
        now = time.time()
        conn.execute(
            "INSERT INTO streams (name, data, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, json.dumps(data), now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, name, data, created_at, updated_at FROM streams WHERE name = ?",
            (name,),
        ).fetchone()
        return _row_to_dict(row)

    def _sync_update(self, stream_id: int, name: Optional[str], data: Optional[dict]) -> Optional[dict]:
        conn = self._open()
        row = conn.execute(
            "SELECT id, name, data, created_at, updated_at FROM streams WHERE id = ?",
            (stream_id,),
        ).fetchone()
        if row is None:
            return None
        now = time.time()
        new_name = name if name is not None else row["name"]
        new_data = json.dumps(data) if data is not None else row["data"]
        conn.execute(
            "UPDATE streams SET name = ?, data = ?, updated_at = ? WHERE id = ?",
            (new_name, new_data, now, stream_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, name, data, created_at, updated_at FROM streams WHERE id = ?",
            (stream_id,),
        ).fetchone()
        return _row_to_dict(row)

    def _sync_get(self, stream_id: int) -> Optional[dict]:
        conn = self._open()
        row = conn.execute(
            "SELECT id, name, data, created_at, updated_at FROM streams WHERE id = ?",
            (stream_id,),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def _sync_delete(self, stream_id: int) -> bool:
        conn = self._open()
        cur = conn.execute("DELETE FROM streams WHERE id = ?", (stream_id,))
        conn.commit()
        return cur.rowcount > 0

    # ── Async public API ──────────────────────────────────────────────────────

    async def list(self) -> list[dict]:
        return await asyncio.to_thread(self._sync_list)

    async def create(self, name: str, data: dict) -> dict:
        return await asyncio.to_thread(self._sync_create, name, data)

    async def update(self, stream_id: int, name: Optional[str] = None, data: Optional[dict] = None) -> Optional[dict]:
        return await asyncio.to_thread(self._sync_update, stream_id, name, data)

    async def get(self, stream_id: int) -> Optional[dict]:
        return await asyncio.to_thread(self._sync_get, stream_id)

    async def delete(self, stream_id: int) -> bool:
        return await asyncio.to_thread(self._sync_delete, stream_id)
