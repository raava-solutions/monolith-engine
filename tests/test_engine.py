"""Engine facade — proof primitives over provider + storage (U3)."""

from __future__ import annotations

import pytest

from monolith_engine import Engine, SqliteStorage
from monolith_engine.ports.provider import ExecResult
from tests.conftest import FakeProvider


@pytest.fixture
def engine(tmp_path):
    return Engine(provider=FakeProvider(), storage=SqliteStorage(tmp_path / "state.db"))


async def test_provision_creates_and_persists(engine):
    rec = await engine.provision("agent-1", image="ubuntu:24.04", role="coder")
    assert rec.id == "agent-1"
    assert rec.status == "running"
    # persisted and readable back through storage
    listed = await engine.status()
    assert [c.id for c in listed] == ["agent-1"]


async def test_provision_autonames_when_unnamed(engine):
    rec = await engine.provision(image="ubuntu:24.04")
    assert rec.id.startswith("agent-")


async def test_chat_oneshots_into_resolved_container(engine):
    engine.provider.exec_result = ExecResult(stdout="DOGFOOD-OK\n", stderr="", exit_code=0)
    await engine.provision("agent-1", image="ubuntu:24.04")
    reply = await engine.chat("agent-1", "echo DOGFOOD-OK")
    assert "DOGFOOD-OK" in reply


async def test_chat_unknown_container_raises(engine):
    with pytest.raises(LookupError):
        await engine.chat("nope", "echo hi")


async def test_chat_failed_exec_raises(engine):
    engine.provider.exec_result = ExecResult(stdout="", stderr="boom", exit_code=2)
    await engine.provision("agent-1", image="ubuntu:24.04")
    with pytest.raises(RuntimeError):
        await engine.chat("agent-1", "false")


async def test_delete_removes_and_marks(engine):
    await engine.provision("agent-1", image="ubuntu:24.04")
    await engine.delete("agent-1")
    assert await engine.status() == []
    assert "agent-1" not in engine.provider.containers


async def test_chat_foreign_tenant_refused(tmp_path):
    store = SqliteStorage(tmp_path / "state.db")
    other = Engine(provider=FakeProvider(), storage=store, tenant_id="other")
    await other.provision("agent-x", image="ubuntu:24.04")
    local = Engine(provider=FakeProvider(), storage=store, tenant_id="local")
    with pytest.raises(LookupError):
        await local.chat("agent-x", "echo hi")
