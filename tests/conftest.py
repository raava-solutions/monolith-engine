"""Shared test fakes for the engine."""

from __future__ import annotations

from dataclasses import dataclass, field

from monolith_engine.ports.provider import ExecResult, VMState


class FakeProvider:
    """In-memory ComputeProvider double — no docker daemon needed."""

    def __init__(self):
        self.containers: dict[str, dict] = {}
        self.exec_result = ExecResult(stdout="ok\n", stderr="", exit_code=0)

    async def create(self, name, image, vcpus, memory_mb, disk_gb, metadata):
        self.containers[name] = {"image": image, "status": "running", "ip": "172.17.0.2"}
        return "172.17.0.2"

    async def get_state(self, name):
        c = self.containers.get(name)
        if not c:
            return VMState(name=name, status="error")
        return VMState(name=name, status=c["status"], ip=c["ip"], provider_id=name)

    async def exists(self, name):
        return name in self.containers

    async def delete(self, name):
        self.containers.pop(name, None)

    async def exec_command(self, name, command, timeout=30, input_bytes=None):
        return self.exec_result

    # Unused-by-proof methods kept as no-ops so the double satisfies callers.
    async def start(self, name): ...
    async def stop(self, name, force=False): ...
    async def list_vms(self): return list(self.containers)
    async def get_ip(self, name): return self.containers.get(name, {}).get("ip")



def pytest_addoption(parser):
    parser.addoption(
        "--live-docker", action="store_true", default=False,
        help="Run tests that create/delete real Docker containers (explicit opt-in)",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live-docker"):
        return
    import pytest

    skip = pytest.mark.skip(reason="requires explicit --live-docker opt-in")
    for item in items:
        if "live_docker" in item.keywords:
            item.add_marker(skip)
