"""Hermes agent one-shot — build and run the in-container agent chat command.

Carved from the hosted repo's HTTP chat router so the agent-dispatch primitive
lives at the engine layer (the in-process CLI and the hosted API both call
this). The command sources the runtime env file, drops to the ``agent`` user,
and invokes the Hermes CLI in one-shot quiet mode.
"""

from __future__ import annotations

import shlex

from monolith_engine.ports.provider import ComputeProvider

_HERMES_DIR = "/home/agent/hermes-agent"
_HERMES_PYTHON = f"{_HERMES_DIR}/venv/bin/python"
_HERMES_HOME = "/home/agent/.hermes"
_PRESERVE_ENV = (
    "OPENROUTER_API_KEY,OPENROUTER_BASE_URL,OPENAI_API_KEY,OPENAI_BASE_URL,ANTHROPIC_API_KEY"
)


def build_oneshot_command(container_id: str, message: str) -> list[str]:
    """The in-container command that relays one message to the Hermes runtime."""
    env_file = f"/etc/hermes/{shlex.quote(container_id)}.env"
    inner = (
        f"set -a; [ -f {env_file} ] && . {env_file}; set +a; "
        f"cd {_HERMES_DIR} && "
        f"exec sudo -u agent --preserve-env={_PRESERVE_ENV} "
        f"env HOME=/home/agent HERMES_HOME={_HERMES_HOME} "
        f"PYTHONPATH={_HERMES_DIR} "
        f"{_HERMES_PYTHON} -m hermes_cli.main chat -q {shlex.quote(message)} -Q"
    )
    return ["sh", "-c", inner]


async def runtime_is_running(provider: ComputeProvider, container_id: str) -> bool:
    """Probe the agent gateway process directly (works with or without systemd)."""
    try:
        result = await provider.exec_command(
            container_id,
            [
                "sh",
                "-c",
                "pgrep -f 'hermes_cli.main' >/dev/null 2>&1 || systemctl is-active --quiet hermes-gateway",
            ],
            timeout=10,
        )
    except Exception:
        return False
    return result.exit_code == 0


async def agent_chat(
    provider: ComputeProvider,
    container_id: str,
    message: str,
    *,
    timeout: int = 120,
) -> str:
    """Relay one message to the agent runtime and return its reply.

    Raises LookupError-equivalent semantics via RuntimeError subclasses:
    - RuntimeError("runtime_not_running: ...") when the gateway is down
    - RuntimeError on a failed dispatch
    """
    if not await runtime_is_running(provider, container_id):
        raise RuntimeError(
            f"runtime_not_running: agent runtime is not running in '{container_id}'"
        )
    result = await provider.exec_command(
        container_id, build_oneshot_command(container_id, message), timeout=timeout
    )
    if result.exit_code != 0:
        raise RuntimeError(
            f"agent dispatch failed (exit {result.exit_code}): "
            f"{(result.stderr or result.stdout or '').strip()[:400]}"
        )
    return result.stdout


__all__ = ["agent_chat", "build_oneshot_command", "runtime_is_running"]
