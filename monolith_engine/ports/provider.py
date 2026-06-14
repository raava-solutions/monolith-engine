"""Provider port — the only contract Fleet API talks to compute backends through.

Adapters in this package (`lxd.py`, `gce.py`, `ec2.py`) implement
`ComputeProvider`. Callers outside the package never import an adapter
directly; they go through `fleet.providers.registry.get_provider`.

This module owns the canonical types (`VMState`, `SnapshotInfo`,
`ExecResult`, `DirectoryEntry`) used at the port boundary.
"""

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, TypedDict


@dataclass
class VMState:
    """Normalized VM state returned by all providers."""

    name: str
    status: str  # running, stopped, error
    ip: str | None = None
    cpu_percent: float = 0.0
    mem_percent: float = 0.0
    mem_bytes: int = 0
    mem_total: int = 0
    disk_percent: float = 0.0
    disk_bytes: int = 0
    disk_total: int = 0
    agent_pid: int | None = None
    agent_status: str = "unknown"
    net_rx_bytes: int = 0
    net_tx_bytes: int = 0
    zone: str | None = None
    provider_id: str | None = None


@dataclass
class SnapshotInfo:
    name: str
    created_at: str
    size_bytes: int | None = None
    status: str = "available"
    stateful: bool = False


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int


class DirectoryEntry(TypedDict):
    name: str
    type: Literal["dir", "file"]
    size: str
    permissions: str
    modified: str


class ComputeProvider(ABC):
    """Provider port. Adapters implement every abstract method."""

    @abstractmethod
    async def create(
        self,
        name: str,
        image: str,
        vcpus: int,
        memory_mb: int,
        disk_gb: int,
        metadata: dict[str, str],
    ) -> str:
        """Create and start a new VM. Return the VM's IP when ready."""

    @abstractmethod
    async def start(self, name: str) -> None: ...

    @abstractmethod
    async def stop(self, name: str, force: bool = False) -> None: ...

    @abstractmethod
    async def pause(self, name: str) -> None: ...

    @abstractmethod
    async def resume(self, name: str) -> None: ...

    @abstractmethod
    async def restart(self, name: str) -> None: ...

    @abstractmethod
    async def delete(self, name: str) -> None: ...

    @abstractmethod
    async def get_state(self, name: str) -> VMState: ...

    @abstractmethod
    async def get_ip(self, name: str) -> str | None: ...

    @abstractmethod
    async def exists(self, name: str) -> bool: ...

    @abstractmethod
    async def list_vms(self) -> list[str]: ...

    @abstractmethod
    async def exec_command(
        self,
        name: str,
        command: list[str],
        timeout: int = 30,
        input_bytes: bytes | None = None,
    ) -> ExecResult: ...

    @abstractmethod
    async def exec_interactive(
        self, name: str, command: list[str] | None = None
    ) -> asyncio.subprocess.Process: ...

    @abstractmethod
    async def read_file(self, name: str, path: str) -> bytes: ...

    @abstractmethod
    async def write_file(self, name: str, path: str, content: bytes) -> None: ...

    @abstractmethod
    async def list_directory(self, name: str, path: str) -> list[DirectoryEntry]: ...

    @abstractmethod
    async def delete_file(self, name: str, path: str) -> None: ...

    @abstractmethod
    async def create_snapshot(self, name: str, snapshot_name: str) -> SnapshotInfo: ...

    @abstractmethod
    async def restore_snapshot(self, name: str, snapshot_name: str) -> None: ...

    @abstractmethod
    async def delete_snapshot(self, name: str, snapshot_name: str) -> None: ...

    @abstractmethod
    async def list_snapshots(self, name: str) -> list[SnapshotInfo]: ...

    @abstractmethod
    async def set_config(self, name: str, key: str, value: str) -> None: ...

    @abstractmethod
    async def push_file(self, local_path: str, name: str, remote_path: str) -> None: ...


__all__ = [
    "ComputeProvider",
    "DirectoryEntry",
    "ExecResult",
    "SnapshotInfo",
    "VMState",
]
