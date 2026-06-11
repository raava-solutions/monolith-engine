"""Default step runner — executes real work against a tenant-scoped target.

Deterministic steps run their shell command in the target container via the
provider port (exit code + bounded output tails persisted); agent steps
dispatch their prompt through the Hermes one-shot. Fail-closed: a step with no
resolvable target, or a target outside the run's tenant, fails before any
provider call. Faithful port of the hosted runner onto the engine ports.
"""

from __future__ import annotations

import os
import time

from monolith_engine.chat.hermes import build_oneshot_command
from monolith_engine.ports.provider import ComputeProvider
from monolith_engine.storage.interface import StorageBackend
from monolith_engine.workflows.fsm import StepContext, StepOutcome

STEP_TIMEOUT_SECONDS = int(os.getenv("WORKFLOW_STEP_TIMEOUT_SECONDS", "300"))
OUTPUT_TAIL_CHARS = 4096


def _tail(text: str | None) -> tuple[str, bool]:
    raw = text or ""
    if len(raw) <= OUTPUT_TAIL_CHARS:
        return raw, False
    return raw[-OUTPUT_TAIL_CHARS:], True


def make_step_runner(storage: StorageBackend, provider: ComputeProvider):
    """Bind the ports into a StepRunner the FSM can drive."""

    async def _resolve_target(ctx: StepContext):
        name = ctx.step.target or ctx.workflow_target
        if not name:
            return None, (
                "target_required: step needs a 'target' container (set it on the "
                "step or at the workflow level)"
            )
        record = await storage.get_container(name, tenant_id=ctx.tenant_id)
        if record is None or record.status == "deleted":
            return None, f"target_not_found: container '{name}' not found"
        return record, None

    async def run_step(ctx: StepContext) -> StepOutcome:
        step = ctx.step
        record, target_error = await _resolve_target(ctx)
        if record is None:
            return StepOutcome(status="failed", error=target_error)
        target = record.id

        if step.type == "deterministic":
            command = ["sh", "-c", step.run or ""]
        else:
            command = build_oneshot_command(target, step.prompt or "")

        started = time.monotonic()
        try:
            result = await provider.exec_command(target, command, timeout=STEP_TIMEOUT_SECONDS)
        except TimeoutError:
            return StepOutcome(
                status="timed_out",
                error=f"step timed out after {STEP_TIMEOUT_SECONDS}s in '{target}'",
                result={"duration_ms": int((time.monotonic() - started) * 1000)},
            )
        duration_ms = int((time.monotonic() - started) * 1000)

        if step.type == "deterministic":
            stdout_tail, stdout_trunc = _tail(result.stdout)
            stderr_tail, stderr_trunc = _tail(result.stderr)
            payload = {
                "exit_code": result.exit_code,
                "stdout_tail": stdout_tail,
                "stdout_truncated": stdout_trunc,
                "stderr_tail": stderr_tail,
                "stderr_truncated": stderr_trunc,
                "duration_ms": duration_ms,
            }
            if result.exit_code == 0:
                return StepOutcome(status="completed", result=payload)
            return StepOutcome(status="failed", result=payload, error=f"command exited {result.exit_code}")

        reply = (result.stdout or "").strip()
        if result.exit_code != 0 or not reply:
            stderr_tail, _ = _tail(result.stderr)
            return StepOutcome(
                status="failed",
                result={"exit_code": result.exit_code, "stderr_tail": stderr_tail, "duration_ms": duration_ms},
                error=f"agent dispatch failed (exit {result.exit_code})",
            )
        reply_tail, reply_trunc = _tail(reply)
        return StepOutcome(
            status="completed",
            result={"reply_tail": reply_tail, "reply_truncated": reply_trunc, "duration_ms": duration_ms},
        )

    return run_step


__all__ = ["make_step_runner", "STEP_TIMEOUT_SECONDS", "OUTPUT_TAIL_CHARS"]
