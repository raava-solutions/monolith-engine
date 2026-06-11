"""monolith-engine — the open single-node engine.

Runs Monolith's core primitives in-process with no server: container
provider, local storage, chat-exec. The CLI imports this for local; the
closed hosted service imports the same engine for cloud, supplying its own
provider/storage/secrets backends behind the identical ports.
"""

from monolith_engine.engine import Engine
from monolith_engine.ports.provider import ComputeProvider, ExecResult, VMState
from monolith_engine.providers.registry import get_provider
from monolith_engine.storage.interface import ContainerRecord, StorageBackend
from monolith_engine.storage.sqlite_backend import SqliteStorage

__version__ = "0.1.0"

__all__ = [
    "Engine",
    "ComputeProvider",
    "ExecResult",
    "VMState",
    "get_provider",
    "ContainerRecord",
    "StorageBackend",
    "SqliteStorage",
]
