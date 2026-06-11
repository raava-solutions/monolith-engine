"""Docker provider — live tier (opt-in, real docker daemon).

Skipped unless a docker daemon is reachable. Proves the carved provider
drives real containers identically to its pre-carve behavior.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid

import pytest

from monolith_engine.providers.registry import get_provider


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, timeout=10,
        ).returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _docker_ready(), reason="docker daemon not available")


async def test_create_exec_delete_roundtrip():
    provider = get_provider("docker")
    name = f"engine-test-{uuid.uuid4().hex[:8]}"
    try:
        await provider.create(name, "ubuntu:24.04", 1, 256, 5, {"test": "1"})
        assert await provider.exists(name)
        result = await provider.exec_command(name, ["echo", "ENGINE-OK"])
        assert result.exit_code == 0
        assert "ENGINE-OK" in result.stdout
    finally:
        if await provider.exists(name):
            await provider.delete(name)
        assert not await provider.exists(name)
