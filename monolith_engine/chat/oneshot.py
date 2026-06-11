"""Chat one-shot exec — relay a message to a container and return the reply.

This is the engine-side primitive that the hosted repo previously buried in
its HTTP chat router (the engine→router layering violation noted in the plan).
It lives at the engine layer so both the in-process CLI and the hosted API
dispatch through the same code.

Ring 0 proof: a "chat" is a one-shot shell exec of the message in the target
container (proving the provider exec port end-to-end in-process). Ring 1
swaps in the full Hermes/OpenClaw agent one-shot builder behind this same
seam — the port (provider.exec_command) is unchanged.
"""

from __future__ import annotations

from monolith_engine.ports.provider import ComputeProvider


async def chat_oneshot(
    provider: ComputeProvider,
    container_id: str,
    message: str,
    *,
    timeout: int = 60,
) -> str:
    """Relay one message to the container and return stdout.

    Raises RuntimeError with stderr on a non-zero exit so the caller can
    surface a typed failure rather than a silent empty reply.
    """
    result = await provider.exec_command(container_id, ["sh", "-c", message], timeout=timeout)
    if result.exit_code != 0:
        raise RuntimeError(
            f"chat exec failed (exit {result.exit_code}): "
            f"{(result.stderr or result.stdout or '').strip()}"
        )
    return result.stdout


__all__ = ["chat_oneshot"]
