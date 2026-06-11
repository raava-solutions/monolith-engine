"""Storage port — the single-node subset of state the engine persists.

The open engine defines this narrow interface (container records + audit for
the proof slice; provision jobs, workflow runs/steps, and connections are
added as the carve widens in Ring 1). The engine ships a SQLite backend; the
closed hosted service implements the same interface over Postgres.

This is deliberately a small slice — the hosted db.py has ~150 functions, but
the single-node engine needs only this handful.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# Local single-node deployments collapse multi-tenancy to one fixed identity;
# the hosted backend passes the real tenant. The engine keeps the parameter so
# both backends share one interface.
LOCAL_TENANT = "local"


@dataclass
class ContainerRecord:
    id: str
    tenant_id: str = LOCAL_TENANT
    image: str = ""
    status: str = "stopped"
    ip: str | None = None
    role: str = ""
    provider: str = "docker"
    created_at: str = ""
    metadata: dict[str, str] = field(default_factory=dict)


class StorageBackend(ABC):
    """The persistence port the engine talks to. Backends: SQLite (local), Postgres (hosted)."""

    @abstractmethod
    async def upsert_container(self, record: ContainerRecord) -> None: ...

    @abstractmethod
    async def get_container(self, container_id: str, *, tenant_id: str = LOCAL_TENANT) -> ContainerRecord | None: ...

    @abstractmethod
    async def list_containers(self, *, tenant_id: str = LOCAL_TENANT, include_deleted: bool = False) -> list[ContainerRecord]: ...

    @abstractmethod
    async def update_container_fields(self, container_id: str, fields: dict, *, tenant_id: str = LOCAL_TENANT) -> None: ...

    @abstractmethod
    async def delete_container(self, container_id: str, *, tenant_id: str = LOCAL_TENANT) -> None: ...

    @abstractmethod
    async def write_audit(self, container_id: str | None, action: str, detail: str) -> None: ...

    # ── Workflows (file-first TOML definitions + runs) ──────────────────────

    @abstractmethod
    async def apply_workflow(
        self, *, tenant_id: str, name: str, body: str, concurrency: str
    ) -> dict:
        """Idempotent on content hash. Returns {workflow_id, version_id, version, created}."""

    @abstractmethod
    async def list_workflows(self, tenant_id: str) -> list[dict]: ...

    @abstractmethod
    async def get_workflow(self, tenant_id: str, name: str) -> dict | None: ...

    @abstractmethod
    async def get_workflow_version(self, version_id: str) -> dict | None: ...

    @abstractmethod
    async def create_run(
        self, *, tenant_id: str, workflow_id: str, version_id: str, steps: list[dict]
    ) -> dict: ...

    @abstractmethod
    async def get_run(self, run_id: str) -> dict | None: ...

    @abstractmethod
    async def get_run_for_tenant(self, tenant_id: str, run_id: str) -> dict | None: ...

    @abstractmethod
    async def mark_run_running(self, run_id: str) -> None: ...

    @abstractmethod
    async def set_run_terminal(self, run_id: str, status: str, *, error: str | None = None) -> None: ...

    @abstractmethod
    async def advance_current_step(self, run_id: str, step_index: int) -> None: ...

    @abstractmethod
    async def heartbeat_run(self, run_id: str) -> None: ...

    @abstractmethod
    async def get_step(self, run_id: str, step_index: int) -> dict | None: ...

    @abstractmethod
    async def list_steps(self, run_id: str) -> list[dict]: ...

    @abstractmethod
    async def set_step_dispatched(self, run_id: str, step_index: int, attempt: int) -> None: ...

    @abstractmethod
    async def set_step_terminal(
        self, run_id: str, step_index: int, status: str, *, result: dict | None = None, error: str | None = None
    ) -> None: ...

    # ── Connections (vault-routed external sources) ─────────────────────────

    @abstractmethod
    async def upsert_connection(
        self, *, tenant_id: str, name: str, connector_type: str, config: dict
    ) -> dict: ...

    @abstractmethod
    async def list_connections(self, tenant_id: str) -> list[dict]: ...

    @abstractmethod
    async def get_connection(self, tenant_id: str, name: str) -> dict | None: ...


__all__ = ["ContainerRecord", "StorageBackend", "LOCAL_TENANT"]
