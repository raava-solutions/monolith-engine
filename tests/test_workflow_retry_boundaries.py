"""Retry and terminal-state contracts using real local persistence."""
import pytest
from monolith_engine.storage.sqlite_backend import SqliteStorage
from monolith_engine.workflows.fsm import StepOutcome, dispatch_run, execute_run
from monolith_engine.workflows.schema import StepSpec, WorkflowSpec

@pytest.fixture
async def workflow(tmp_path):
    storage = SqliteStorage(tmp_path / "workflow.db")
    spec = WorkflowSpec(name="retry", steps=[StepSpec(name="work", type="deterministic", run="echo work", retry_budget=2)])
    applied = await storage.apply_workflow(tenant_id="local", name=spec.name, body="fixture", concurrency="allow")
    run = await dispatch_run(storage, tenant_id="local", spec=spec, workflow_id=applied["workflow_id"], version_id=applied["version_id"])
    return storage, spec, run["id"]

@pytest.mark.parametrize("status", ["succeeded", "failed", "timed_out", "cancelled"])
async def test_terminal_run_is_read_only(workflow, status):
    storage, spec, run_id = workflow
    await storage.set_run_terminal(run_id, status, error="original")
    before = await storage.get_run(run_id)
    async def runner(ctx):
        pytest.fail("terminal run must not execute again")
    assert await execute_run(storage, run_id, spec, runner) == status
    assert await storage.get_run(run_id) == before
    assert (await storage.get_step(run_id, 0))["status"] == "pending"

@pytest.mark.parametrize("raises", [False, True])
async def test_cancelled_attempt_does_not_retry(workflow, raises):
    storage, spec, run_id = workflow
    attempts = []
    async def runner(ctx):
        attempts.append(ctx.attempt)
        await storage.set_run_terminal(run_id, "cancelled")
        if raises:
            raise RuntimeError("interrupted")
        return StepOutcome(status="failed", error="retryable")
    assert await execute_run(storage, run_id, spec, runner) == "cancelled"
    assert attempts == [0]
    assert (await storage.get_step(run_id, 0))["attempt"] == 0
    assert (await storage.get_run(run_id))["status"] == "cancelled"

@pytest.mark.parametrize("succeed_at", [1, None])
async def test_retry_budget_and_success_are_preserved(workflow, succeed_at):
    storage, spec, run_id = workflow
    attempts = []
    async def runner(ctx):
        attempts.append(ctx.attempt)
        return StepOutcome(status="completed" if ctx.attempt == succeed_at else "failed")
    expected = "succeeded" if succeed_at is not None else "failed"
    assert await execute_run(storage, run_id, spec, runner) == expected
    assert attempts == ([0, 1] if succeed_at is not None else [0, 1, 2])
