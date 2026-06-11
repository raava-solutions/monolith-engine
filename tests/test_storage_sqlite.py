"""SQLite storage backend — the single-node local persistence contract (U2)."""

from __future__ import annotations

import pytest

from monolith_engine.storage.interface import ContainerRecord
from monolith_engine.storage.sqlite_backend import SqliteStorage


@pytest.fixture
def store(tmp_path):
    return SqliteStorage(tmp_path / "state.db")


async def test_upsert_then_get_roundtrips(store):
    rec = ContainerRecord(id="agent-1", image="ubuntu:24.04", status="running", ip="172.17.0.2", role="agent")
    await store.upsert_container(rec)
    got = await store.get_container("agent-1")
    assert got is not None
    assert got.image == "ubuntu:24.04"
    assert got.status == "running"
    assert got.ip == "172.17.0.2"


async def test_update_fields_persists(store):
    await store.upsert_container(ContainerRecord(id="agent-1", status="running"))
    await store.update_container_fields("agent-1", {"status": "stopped", "ip": "10.0.0.9"})
    got = await store.get_container("agent-1")
    assert got.status == "stopped" and got.ip == "10.0.0.9"


async def test_list_excludes_deleted_by_default(store):
    await store.upsert_container(ContainerRecord(id="a", status="running"))
    await store.upsert_container(ContainerRecord(id="b", status="running"))
    await store.delete_container("b")
    live = await store.list_containers()
    ids = {c.id for c in live}
    assert ids == {"a"}
    assert {c.id for c in await store.list_containers(include_deleted=True)} == {"a", "b"}


async def test_fresh_file_autocreates_schema(tmp_path):
    # A brand-new path must work without any migration step.
    s = SqliteStorage(tmp_path / "nested" / "fresh.db")
    assert await s.list_containers() == []


async def test_tenant_scoping_isolates(store):
    await store.upsert_container(ContainerRecord(id="a", tenant_id="local"))
    await store.upsert_container(ContainerRecord(id="b", tenant_id="other"))
    assert {c.id for c in await store.list_containers(tenant_id="local")} == {"a"}
    assert await store.get_container("b", tenant_id="local") is None


async def test_audit_write_does_not_error(store):
    await store.write_audit("agent-1", "provision", "image=x")
    # No read API in the proof subset; the contract is "persists without error".
