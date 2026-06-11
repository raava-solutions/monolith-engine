"""Engine facade — the in-process primitives the CLI and hosted service share.

Composes the provider + storage ports into the core single-node operations.
Constructed with a provider and a storage backend; the local CLI builds it
with DockerProvider + SqliteStorage (no server), the hosted service builds it
with its cloud provider + Postgres backend. Same code, different backends —
this is the parity guarantee in one class.

Ring 0 covers the proof primitives: provision (create + persist), chat
(one-shot exec), status (list), delete. The full provisioner (template
render + provision.sh) and workflow engine land in Ring 1 behind the same
ports.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from monolith_engine.chat.oneshot import chat_oneshot
from monolith_engine.ports.provider import ComputeProvider
from monolith_engine.storage.interface import (
    LOCAL_TENANT,
    ContainerRecord,
    StorageBackend,
)


@dataclass
class Engine:
    provider: ComputeProvider
    storage: StorageBackend
    tenant_id: str = LOCAL_TENANT

    async def provision(
        self,
        name: str | None = None,
        *,
        image: str = "",
        role: str = "agent",
        vcpus: int = 1,
        memory_mb: int = 512,
        disk_gb: int = 10,
    ) -> ContainerRecord:
        """Create a container via the provider and persist it via storage."""
        container_id = name or f"agent-{uuid.uuid4().hex[:10]}"
        await self.provider.create(
            container_id, image, vcpus, memory_mb, disk_gb,
            {"com.thisismonolith.tenant": self.tenant_id, "com.thisismonolith.role": role},
        )
        state = await self.provider.get_state(container_id)
        record = ContainerRecord(
            id=container_id,
            tenant_id=self.tenant_id,
            image=image,
            status=state.status,
            ip=state.ip,
            role=role,
            provider="docker",
            created_at=datetime.now(UTC).isoformat(),
        )
        await self.storage.upsert_container(record)
        await self.storage.write_audit(container_id, "provision", f"image={image} role={role}")
        return record

    async def chat(self, container_id: str, message: str, *, timeout: int = 60) -> str:
        """Resolve a tenant-scoped container, then one-shot the message into it."""
        record = await self.storage.get_container(container_id, tenant_id=self.tenant_id)
        if record is None or record.status == "deleted":
            raise LookupError(f"container '{container_id}' not found")
        return await chat_oneshot(self.provider, record.id, message, timeout=timeout)

    async def status(self) -> list[ContainerRecord]:
        """List the tenant's live containers."""
        return await self.storage.list_containers(tenant_id=self.tenant_id)

    async def delete(self, container_id: str) -> None:
        """Remove the container and mark it deleted in storage (fail-closed on tenant)."""
        record = await self.storage.get_container(container_id, tenant_id=self.tenant_id)
        if record is None or record.status == "deleted":
            raise LookupError(f"container '{container_id}' not found")
        if await self.provider.exists(record.id):
            await self.provider.delete(record.id)
        await self.storage.delete_container(record.id, tenant_id=self.tenant_id)
        await self.storage.write_audit(record.id, "delete", "engine.delete")


__all__ = ["Engine"]
