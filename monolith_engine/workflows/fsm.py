"""Workflow run engine — the FSM driver, ported onto the storage port.

Drives a run through its step sequence with retry budgets, ``on_failure``
policy, cooperative cancellation, and durable resume-after-last-committed-step.
Faithful port of the hosted FSM; the only change is persistence goes through
``StorageBackend`` instead of a Postgres repo, so the same FSM runs on SQLite
(local, in-process) and Postgres (hosted).

Run states:  queued → running → succeeded | failed | cancelled | timed_out
Step states: pending → dispatched → completed | failed | timed_out | cancelled
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from monolith_engine.storage.interface import StorageBackend
from monolith_engine.workflows.schema import StepSpec, WorkflowSpec

TERMINAL_RUN_STATUSES = {"succeeded", "failed", "cancelled", "timed_out"}


@dataclass
class StepContext:
    run_id: str
    tenant_id: str
    step_index: int
    step: StepSpec
    attempt: int
    workflow_target: str | None = None


@dataclass
class StepOutcome:
    status: str  # completed | failed | timed_out
    result: dict | None = None
    error: str | None = None


StepRunner = Callable[[StepContext], Awaitable[StepOutcome]]


def _run_terminal_for_step(step_status: str) -> str:
    return "timed_out" if step_status == "timed_out" else "failed"


async def dispatch_run(
    storage: StorageBackend,
    *,
    tenant_id: str,
    workflow_id: str,
    version_id: str,
    spec: WorkflowSpec,
) -> dict:
    """Create a queued run + its step rows from a validated spec."""
    steps = [{"name": s.name, "type": s.type} for s in spec.steps]
    return await storage.create_run(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        version_id=version_id,
        steps=steps,
    )


async def execute_run(
    storage: StorageBackend, run_id: str, spec: WorkflowSpec, runner: StepRunner
) -> str:
    """Drive a run to a terminal state. Resumes after the last committed step."""
    run = await storage.get_run(run_id)
    if run is None:
        raise LookupError(f"run {run_id} not found")
    tenant_id = run["tenant_id"]
    steps = spec.steps
    start_index = int(run["current_step"])

    for idx in range(start_index, len(steps)):
        current = await storage.get_run(run_id)
        if current is None or current["status"] == "cancelled":
            return "cancelled"

        step_row = await storage.get_step(run_id, idx)
        if step_row is not None and step_row["status"] == "completed":
            continue  # resume: already committed

        step_spec = steps[idx]
        outcome: StepOutcome | None = None
        for attempt in range(step_spec.retry_budget + 1):
            await storage.set_step_dispatched(run_id, idx, attempt)
            await storage.heartbeat_run(run_id)
            try:
                outcome = await runner(
                    StepContext(
                        run_id=run_id,
                        tenant_id=tenant_id,
                        step_index=idx,
                        step=step_spec,
                        attempt=attempt,
                        workflow_target=spec.target,
                    )
                )
            except Exception as exc:  # runner crash → failed attempt
                outcome = StepOutcome(status="failed", error=str(exc))
            if outcome.status == "completed":
                break

        assert outcome is not None

        # Late cancellation wins over a terminal write.
        latest = await storage.get_run(run_id)
        if latest is not None and latest["status"] == "cancelled":
            return "cancelled"
        if outcome.status == "completed":
            await storage.set_step_terminal(run_id, idx, "completed", result=outcome.result)
            await storage.advance_current_step(run_id, idx + 1)
            continue

        # Failed/timed_out after the retry budget — persist the result payload
        # too (exit codes and output tails matter most on failures).
        await storage.set_step_terminal(
            run_id, idx, outcome.status, result=outcome.result, error=outcome.error
        )
        if step_spec.on_failure == "continue":
            await storage.advance_current_step(run_id, idx + 1)
            continue
        terminal = _run_terminal_for_step(outcome.status)
        await storage.set_run_terminal(run_id, terminal, error=outcome.error)
        return terminal

    final = await storage.get_run(run_id)
    if final is not None and final["status"] == "cancelled":
        return "cancelled"
    await storage.set_run_terminal(run_id, "succeeded")
    return "succeeded"


__all__ = [
    "StepContext",
    "StepOutcome",
    "StepRunner",
    "TERMINAL_RUN_STATUSES",
    "dispatch_run",
    "execute_run",
]
