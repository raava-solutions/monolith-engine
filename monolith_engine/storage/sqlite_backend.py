"""SQLite storage backend — the single-node local persistence.

No server, no Postgres: engine state lives in one SQLite file under the
operator's config dir. Promotes the hosted repo's previously test-only
sqlite-compatible path to a real backend. asyncio-friendly via a thread
executor (sqlite3 itself is sync).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from monolith_engine.storage.interface import (
    LOCAL_TENANT,
    ContainerRecord,
    StorageBackend,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS containers (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'local',
    image TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'stopped',
    ip TEXT,
    role TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT 'docker',
    created_at TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container_id TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);
"""


def _row_to_record(row: sqlite3.Row) -> ContainerRecord:
    return ContainerRecord(
        id=row["id"],
        tenant_id=row["tenant_id"],
        image=row["image"],
        status=row["status"],
        ip=row["ip"],
        role=row["role"],
        provider=row["provider"],
        created_at=row["created_at"],
        metadata=json.loads(row["metadata"] or "{}"),
    )


class SqliteStorage(StorageBackend):
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        p = Path(self.path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    async def _run(self, fn):
        return await asyncio.to_thread(fn)

    async def upsert_container(self, record: ContainerRecord) -> None:
        def op():
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO containers (id, tenant_id, image, status, ip, role, provider, created_at, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        tenant_id=excluded.tenant_id, image=excluded.image, status=excluded.status,
                        ip=excluded.ip, role=excluded.role, provider=excluded.provider, metadata=excluded.metadata
                    """,
                    (
                        record.id, record.tenant_id, record.image, record.status, record.ip,
                        record.role, record.provider,
                        record.created_at or datetime.now(UTC).isoformat(),
                        json.dumps(record.metadata or {}),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        await self._run(op)

    async def get_container(self, container_id: str, *, tenant_id: str = LOCAL_TENANT) -> ContainerRecord | None:
        def op():
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM containers WHERE id=? AND tenant_id=?",
                    (container_id, tenant_id),
                ).fetchone()
                return _row_to_record(row) if row else None
            finally:
                conn.close()
        return await self._run(op)

    async def list_containers(self, *, tenant_id: str = LOCAL_TENANT, include_deleted: bool = False) -> list[ContainerRecord]:
        def op():
            conn = self._connect()
            try:
                q = "SELECT * FROM containers WHERE tenant_id=?"
                if not include_deleted:
                    q += " AND status != 'deleted'"
                rows = conn.execute(q + " ORDER BY created_at DESC", (tenant_id,)).fetchall()
                return [_row_to_record(r) for r in rows]
            finally:
                conn.close()
        return await self._run(op)

    async def update_container_fields(self, container_id: str, fields: dict, *, tenant_id: str = LOCAL_TENANT) -> None:
        if not fields:
            return
        allowed = {"image", "status", "ip", "role", "provider", "metadata"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append(f"{k}=?")
            vals.append(json.dumps(v) if k == "metadata" else v)
        if not sets:
            return
        vals += [container_id, tenant_id]

        def op():
            conn = self._connect()
            try:
                conn.execute(
                    f"UPDATE containers SET {', '.join(sets)} WHERE id=? AND tenant_id=?",
                    vals,
                )
                conn.commit()
            finally:
                conn.close()
        await self._run(op)

    async def delete_container(self, container_id: str, *, tenant_id: str = LOCAL_TENANT) -> None:
        await self.update_container_fields(container_id, {"status": "deleted"}, tenant_id=tenant_id)

    async def write_audit(self, container_id: str | None, action: str, detail: str) -> None:
        def op():
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO audit_log (container_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
                    (container_id, action, detail, datetime.now(UTC).isoformat()),
                )
                conn.commit()
            finally:
                conn.close()
        await self._run(op)


__all__ = ["SqliteStorage"]
