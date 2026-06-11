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

from monolith_engine.chat.hermes import agent_chat
from monolith_engine.connections_registry import get_connector, known_connector_types
from monolith_engine.ports.provider import ComputeProvider
from monolith_engine.storage.interface import (
    LOCAL_TENANT,
    ContainerRecord,
    StorageBackend,
)
from monolith_engine.workflows import fsm
from monolith_engine.workflows.runner import make_step_runner
from monolith_engine.workflows.schema import parse_workflow_toml


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
        """Create a tracked container via the provider and persist it (LOW-LEVEL).

        The product path is template-based provisioning (every container is
        agent-backed); this primitive creates a bare tracked container and is
        used internally/in tests, never exposed as a user-facing provision.
        """
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

    async def chat(self, container_id: str, message: str, *, timeout: int = 120) -> str:
        """Resolve a tenant-scoped container, then dispatch to its agent runtime.

        Falls back to a raw shell one-shot when no agent runtime is present
        (containers provisioned without an agent — the Ring 0 proof shape).
        """
        record = await self.storage.get_container(container_id, tenant_id=self.tenant_id)
        if record is None or record.status == "deleted":
            raise LookupError(f"container '{container_id}' not found")
        # Every container is agent-backed (provisioned from a template), so chat
        # always dispatches to the agent runtime — there is no bare-container path.
        return await agent_chat(self.provider, record.id, message, timeout=timeout)

    # ── Workflows ────────────────────────────────────────────────────────────

    async def workflow_apply(self, body: str) -> dict:
        """Validate and version a TOML workflow definition (idempotent)."""
        spec = parse_workflow_toml(body)
        result = await self.storage.apply_workflow(
            tenant_id=self.tenant_id, name=spec.name, body=body, concurrency=spec.concurrency
        )
        await self.storage.write_audit(None, "workflow:apply", f"name={spec.name} v={result['version']}")
        return {"name": spec.name, **result}

    async def workflow_list(self) -> list[dict]:
        rows = await self.storage.list_workflows(self.tenant_id)
        return [
            {
                "name": r["name"],
                "concurrency": r["concurrency"],
                "enabled": bool(r["enabled"]),
                "current_version_id": r["current_version_id"],
            }
            for r in rows
        ]

    async def workflow_get(self, name: str) -> dict | None:
        wf = await self.storage.get_workflow(self.tenant_id, name)
        if wf is None:
            return None
        version = (
            await self.storage.get_workflow_version(wf["current_version_id"])
            if wf["current_version_id"] else None
        )
        return {
            "name": wf["name"],
            "concurrency": wf["concurrency"],
            "enabled": bool(wf["enabled"]),
            "version": version["version"] if version else None,
            "content_hash": version["content_hash"] if version else None,
            "body": version["body"] if version else None,
        }

    async def workflow_run(self, name: str) -> dict:
        """Dispatch and drive a run of the workflow's current version."""
        wf = await self.storage.get_workflow(self.tenant_id, name)
        if wf is None:
            raise LookupError(f"workflow '{name}' not found")
        if not wf["current_version_id"]:
            raise ValueError("workflow has no applied version")
        version = await self.storage.get_workflow_version(wf["current_version_id"])
        spec = parse_workflow_toml(version["body"])
        run = await fsm.dispatch_run(
            self.storage,
            tenant_id=self.tenant_id,
            workflow_id=wf["id"],
            version_id=version["id"],
            spec=spec,
        )
        await self.storage.mark_run_running(run["id"])
        runner = make_step_runner(self.storage, self.provider)
        final = await fsm.execute_run(self.storage, run["id"], spec, runner)
        await self.storage.write_audit(None, "workflow:run", f"name={name} run={run['id']} status={final}")
        return {"run_id": run["id"], "status": final}

    async def workflow_run_status(self, run_id: str) -> dict | None:
        run = await self.storage.get_run_for_tenant(self.tenant_id, run_id)
        if run is None:
            return None
        steps = await self.storage.list_steps(run_id)
        import json as _json

        def _result(raw):
            if not raw:
                return None
            try:
                parsed = _json.loads(raw) if isinstance(raw, str) else raw
            except (ValueError, TypeError):
                return None
            return parsed if isinstance(parsed, dict) else None

        return {
            "run_id": run["id"],
            "workflow_id": run["workflow_id"],
            "status": run["status"],
            "current_step": run["current_step"],
            "error": run["error"],
            "steps": [
                {
                    "index": s["step_index"],
                    "name": s["name"],
                    "type": s["type"],
                    "status": s["status"],
                    "attempt": s["attempt"],
                    "error": s["error"],
                    "result": _result(s.get("result")),
                }
                for s in steps
            ],
        }

    # ── Connections ──────────────────────────────────────────────────────────

    async def connection_add(self, name: str, connector_type: str, config: dict) -> dict:
        connector = get_connector(connector_type)
        connector.validate_config(config)
        row = await self.storage.upsert_connection(
            tenant_id=self.tenant_id, name=name, connector_type=connector_type, config=config
        )
        await self.storage.write_audit(None, "connection:create", f"name={name} type={connector_type}")
        return {"name": row["name"], "type": row["connector_type"], "status": row["status"]}

    async def connection_list(self) -> dict:
        rows = await self.storage.list_connections(self.tenant_id)
        return {
            "connectors_available": known_connector_types(),
            "connections": [
                {"name": r["name"], "type": r["connector_type"], "status": r["status"]}
                for r in rows
            ],
        }

    async def connection_get(self, name: str) -> dict | None:
        import json as _json

        row = await self.storage.get_connection(self.tenant_id, name)
        if row is None:
            return None
        cfg = row["config"]
        return {
            "name": row["name"],
            "type": row["connector_type"],
            "status": row["status"],
            "config": _json.loads(cfg) if isinstance(cfg, str) else cfg,
        }

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
