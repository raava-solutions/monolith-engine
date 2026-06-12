"""U6 — workflows FSM + connections through the Engine facade on SQLite."""

from __future__ import annotations

import pytest

from monolith_engine import Engine, SqliteStorage
from monolith_engine.ports.provider import ExecResult
from tests.conftest import FakeProvider

WF = """
name = "ci"
target = "agent-1"

[[steps]]
name = "build"
type = "deterministic"
run = "echo built"

[[steps]]
name = "flaky"
type = "deterministic"
run = "false"
on_failure = "continue"

[[steps]]
name = "summarize"
type = "agent"
prompt = "say done"
"""


@pytest.fixture
def engine(tmp_path):
    return Engine(provider=FakeProvider(), storage=SqliteStorage(tmp_path / "s.db"))


async def test_apply_is_idempotent_on_content(engine):
    r1 = await engine.workflow_apply(WF)
    assert r1["created"] is True and r1["version"] == 1
    r2 = await engine.workflow_apply(WF)
    assert r2["created"] is False and r2["version"] == 1
    r3 = await engine.workflow_apply(WF + "\n# changed\n")
    assert r3["created"] is True and r3["version"] == 2


async def test_list_and_get(engine):
    await engine.workflow_apply(WF)
    listed = await engine.workflow_list()
    assert [w["name"] for w in listed] == ["ci"]
    got = await engine.workflow_get("ci")
    assert got["version"] == 1 and 'name = "ci"' in got["body"]
    assert await engine.workflow_get("nope") is None


async def test_run_executes_real_steps_and_persists_results(engine):
    await engine.provision("agent-1", image="ubuntu:24.04")
    await engine.workflow_apply(WF)
    # deterministic exec returns rc=0 first; FakeProvider returns one canned result,
    # so 'flaky' also exits 0 here — override per-call to exercise the failure path.
    calls = {"n": 0}
    results = [
        ExecResult("built\n", "", 0),       # build
        ExecResult("", "no such file", 1),  # flaky (continue)
        ExecResult("done\n", "", 0),        # summarize agent probe (runtime check)
        ExecResult("done\n", "", 0),        # summarize agent dispatch
    ]

    async def exec_command(name, command, timeout=30, input_bytes=None):
        i = min(calls["n"], len(results) - 1)
        calls["n"] += 1
        return results[i]

    engine.provider.exec_command = exec_command

    run = await engine.workflow_run("ci")
    assert run["status"] == "succeeded"

    status = await engine.workflow_run_status(run["run_id"])
    by_name = {s["name"]: s for s in status["steps"]}
    assert by_name["build"]["status"] == "completed"
    assert by_name["build"]["result"]["exit_code"] == 0
    assert "built" in by_name["build"]["result"]["stdout_tail"]
    assert by_name["flaky"]["status"] == "failed"
    assert by_name["flaky"]["result"]["exit_code"] == 1
    assert by_name["summarize"]["status"] == "completed"


async def test_run_fail_run_policy_stops(engine, tmp_path):
    wf = WF.replace('on_failure = "continue"', 'on_failure = "fail_run"')
    await engine.provision("agent-1", image="ubuntu:24.04")
    await engine.workflow_apply(wf)
    engine.provider.exec_result = ExecResult("", "boom", 2)
    # first step also fails (rc=2) → run fails at step 0
    run = await engine.workflow_run("ci")
    assert run["status"] == "failed"


async def test_run_unknown_workflow_raises(engine):
    with pytest.raises(LookupError):
        await engine.workflow_run("ghost")


async def test_run_missing_target_fails_closed(engine):
    # workflow targets agent-1 but it was never provisioned for THIS tenant
    await engine.workflow_apply(WF)
    run = await engine.workflow_run("ci")
    assert run["status"] == "failed"
    status = await engine.workflow_run_status(run["run_id"])
    assert "target_not_found" in (status["steps"][0]["error"] or "")


async def test_run_status_tenant_scoped(engine, tmp_path):
    await engine.provision("agent-1", image="x")
    await engine.workflow_apply(WF)
    run = await engine.workflow_run("ci")
    other = Engine(provider=FakeProvider(), storage=engine.storage, tenant_id="other")
    assert await other.workflow_run_status(run["run_id"]) is None


# ── Connections ───────────────────────────────────────────────────────────────


async def test_connection_add_list_get(engine):
    r = await engine.connection_add("gh", "github", {"token": "env:GITHUB_TOKEN"})
    assert r["status"] == "active"
    listed = await engine.connection_list()
    assert "github" in listed["connectors_available"]
    assert [c["name"] for c in listed["connections"]] == ["gh"]
    got = await engine.connection_get("gh")
    assert got["config"] == {"token": "env:GITHUB_TOKEN"}


async def test_connection_inline_secret_rejected(engine):
    from monolith_engine.connections_registry import ConnectorError

    with pytest.raises(ConnectorError):
        await engine.connection_add("gh", "github", {"token": "ghp_raw"})


async def test_connection_unknown_type_rejected(engine):
    from monolith_engine.connections_registry import ConnectorError

    with pytest.raises(ConnectorError):
        await engine.connection_add("x", "ftp", {})
