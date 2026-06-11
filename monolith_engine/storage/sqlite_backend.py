"""SQLite storage backend — the single-node local persistence.

No server, no Postgres: engine state lives in one SQLite file under the
operator's config dir. Promotes the hosted repo's previously test-only
sqlite-compatible path to a real backend. asyncio-friendly via a thread
executor (sqlite3 itself is sync).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import uuid
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
CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    concurrency TEXT NOT NULL DEFAULT 'singleton',
    enabled INTEGER NOT NULL DEFAULT 1,
    current_version_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(tenant_id, name)
);
CREATE TABLE IF NOT EXISTS workflow_versions (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    current_step INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    heartbeat_at TEXT
);
CREATE TABLE IF NOT EXISTS workflow_run_steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    error TEXT,
    started_at TEXT,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS connections (
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    connector_type TEXT NOT NULL,
    config TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, name)
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



    # ── Workflows ────────────────────────────────────────────────────────────

    async def apply_workflow(self, *, tenant_id: str, name: str, body: str, concurrency: str) -> dict:
        def op():
            chash = hashlib.sha256(body.encode()).hexdigest()
            now = datetime.now(UTC).isoformat()
            conn = self._connect()
            try:
                wf = conn.execute(
                    "SELECT * FROM workflows WHERE tenant_id=? AND name=?", (tenant_id, name)
                ).fetchone()
                if wf is None:
                    workflow_id = uuid.uuid4().hex
                    conn.execute(
                        "INSERT INTO workflows (id, tenant_id, name, concurrency, enabled, created_at) VALUES (?,?,?,?,1,?)",
                        (workflow_id, tenant_id, name, concurrency, now),
                    )
                else:
                    workflow_id = wf["id"]
                    existing = conn.execute(
                        "SELECT * FROM workflow_versions WHERE workflow_id=? AND content_hash=?",
                        (workflow_id, chash),
                    ).fetchone()
                    if existing is not None:
                        conn.commit()
                        return {
                            "workflow_id": workflow_id,
                            "version_id": existing["id"],
                            "version": existing["version"],
                            "created": False,
                        }
                    conn.execute(
                        "UPDATE workflows SET concurrency=? WHERE id=?", (concurrency, workflow_id)
                    )
                row = conn.execute(
                    "SELECT COALESCE(MAX(version),0)+1 AS v FROM workflow_versions WHERE workflow_id=?",
                    (workflow_id,),
                ).fetchone()
                version = row["v"]
                version_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO workflow_versions (id, workflow_id, version, content_hash, body, created_at) VALUES (?,?,?,?,?,?)",
                    (version_id, workflow_id, version, chash, body, now),
                )
                conn.execute(
                    "UPDATE workflows SET current_version_id=? WHERE id=?", (version_id, workflow_id)
                )
                conn.commit()
                return {"workflow_id": workflow_id, "version_id": version_id, "version": version, "created": True}
            finally:
                conn.close()
        return await self._run(op)

    async def list_workflows(self, tenant_id: str) -> list[dict]:
        def op():
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM workflows WHERE tenant_id=? ORDER BY name", (tenant_id,)
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        return await self._run(op)

    async def get_workflow(self, tenant_id: str, name: str) -> dict | None:
        def op():
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM workflows WHERE tenant_id=? AND name=?", (tenant_id, name)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
        return await self._run(op)

    async def get_workflow_version(self, version_id: str) -> dict | None:
        def op():
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM workflow_versions WHERE id=?", (version_id,)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
        return await self._run(op)

    async def create_run(self, *, tenant_id: str, workflow_id: str, version_id: str, steps: list[dict]) -> dict:
        def op():
            run_id = uuid.uuid4().hex
            now = datetime.now(UTC).isoformat()
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO workflow_runs (id, workflow_id, version_id, tenant_id, status, current_step, created_at) VALUES (?,?,?,?, 'queued', 0, ?)",
                    (run_id, workflow_id, version_id, tenant_id, now),
                )
                for idx, st in enumerate(steps):
                    conn.execute(
                        "INSERT INTO workflow_run_steps (id, run_id, tenant_id, step_index, name, type, status) VALUES (?,?,?,?,?,?, 'pending')",
                        (uuid.uuid4().hex, run_id, tenant_id, idx, st["name"], st["type"]),
                    )
                conn.commit()
                row = conn.execute("SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
                return dict(row)
            finally:
                conn.close()
        return await self._run(op)

    async def get_run(self, run_id: str) -> dict | None:
        def op():
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
        return await self._run(op)

    async def get_run_for_tenant(self, tenant_id: str, run_id: str) -> dict | None:
        run = await self.get_run(run_id)
        if run is None or run.get("tenant_id") != tenant_id:
            return None
        return run

    async def mark_run_running(self, run_id: str) -> None:
        def op():
            now = datetime.now(UTC).isoformat()
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE workflow_runs SET status='running', started_at=COALESCE(started_at, ?), heartbeat_at=? WHERE id=?",
                    (now, now, run_id),
                )
                conn.commit()
            finally:
                conn.close()
        await self._run(op)

    async def set_run_terminal(self, run_id: str, status: str, *, error: str | None = None) -> None:
        def op():
            now = datetime.now(UTC).isoformat()
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE workflow_runs SET status=?, error=?, completed_at=? WHERE id=?",
                    (status, error, now, run_id),
                )
                conn.commit()
            finally:
                conn.close()
        await self._run(op)

    async def advance_current_step(self, run_id: str, step_index: int) -> None:
        def op():
            conn = self._connect()
            try:
                conn.execute("UPDATE workflow_runs SET current_step=? WHERE id=?", (step_index, run_id))
                conn.commit()
            finally:
                conn.close()
        await self._run(op)

    async def heartbeat_run(self, run_id: str) -> None:
        def op():
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE workflow_runs SET heartbeat_at=? WHERE id=?",
                    (datetime.now(UTC).isoformat(), run_id),
                )
                conn.commit()
            finally:
                conn.close()
        await self._run(op)

    async def get_step(self, run_id: str, step_index: int) -> dict | None:
        def op():
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM workflow_run_steps WHERE run_id=? AND step_index=?",
                    (run_id, step_index),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
        return await self._run(op)

    async def list_steps(self, run_id: str) -> list[dict]:
        def op():
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM workflow_run_steps WHERE run_id=? ORDER BY step_index",
                    (run_id,),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        return await self._run(op)

    async def set_step_dispatched(self, run_id: str, step_index: int, attempt: int) -> None:
        def op():
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE workflow_run_steps SET status='dispatched', attempt=?, started_at=COALESCE(started_at, ?) WHERE run_id=? AND step_index=?",
                    (attempt, datetime.now(UTC).isoformat(), run_id, step_index),
                )
                conn.commit()
            finally:
                conn.close()
        await self._run(op)

    async def set_step_terminal(self, run_id: str, step_index: int, status: str, *, result: dict | None = None, error: str | None = None) -> None:
        def op():
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE workflow_run_steps SET status=?, result=?, error=?, completed_at=? WHERE run_id=? AND step_index=?",
                    (status, json.dumps(result) if result is not None else None, error,
                     datetime.now(UTC).isoformat(), run_id, step_index),
                )
                conn.commit()
            finally:
                conn.close()
        await self._run(op)

    # ── Connections ──────────────────────────────────────────────────────────

    async def upsert_connection(self, *, tenant_id: str, name: str, connector_type: str, config: dict) -> dict:
        def op():
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT INTO connections (tenant_id, name, connector_type, config, status, created_at)
                       VALUES (?,?,?,?, 'active', ?)
                       ON CONFLICT(tenant_id, name) DO UPDATE SET
                         connector_type=excluded.connector_type, config=excluded.config""",
                    (tenant_id, name, connector_type, json.dumps(config or {}),
                     datetime.now(UTC).isoformat()),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM connections WHERE tenant_id=? AND name=?", (tenant_id, name)
                ).fetchone()
                return dict(row)
            finally:
                conn.close()
        return await self._run(op)

    async def list_connections(self, tenant_id: str) -> list[dict]:
        def op():
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM connections WHERE tenant_id=? ORDER BY name", (tenant_id,)
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        return await self._run(op)

    async def get_connection(self, tenant_id: str, name: str) -> dict | None:
        def op():
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM connections WHERE tenant_id=? AND name=?", (tenant_id, name)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
        return await self._run(op)


__all__ = ["SqliteStorage"]
