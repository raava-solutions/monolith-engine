"""Docker adapter — implements ComputeProvider by shelling out to the Docker CLI.

Containers are treated as "VMs" in Fleet API parlance. Each ``name`` maps to a
Docker container name/ID. Operations shell out to the ``docker`` binary, which
must be on PATH, consistent with how ``services/lxd.py`` shells out to ``lxc``.

Snapshots are ``docker commit`` image tags; restore recreates the container
from the snapshot image (see :meth:`DockerProvider.restore_snapshot`).
Genuinely unsupported operations (runtime ``set_config``) raise
``NotImplementedError`` with a clear message.
"""

import asyncio
import logging
import shlex
from datetime import UTC, datetime
from typing import Any

from monolith_engine.ports.provider import (
    ComputeProvider,
    DirectoryEntry,
    ExecResult,
    SnapshotInfo,
    VMState,
)

logger = logging.getLogger("monolith_engine.providers.docker")

_DOCKER_STATUS_MAP: dict[str, str] = {
    "running": "running",
    "exited": "stopped",
    "created": "creating",
}


def _map_docker_status(raw: str) -> str:
    return _DOCKER_STATUS_MAP.get(raw.lower().strip(), "error")


async def _run(
    args: list[str], timeout: int = 30, input_bytes: bytes | None = None
) -> tuple[str, str, int]:
    """Run a subprocess command, return (stdout, stderr, returncode)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=input_bytes), timeout=float(timeout)
        )
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        raise
    rc = proc.returncode if proc.returncode is not None else 1
    return stdout_b.decode(errors="replace"), stderr_b.decode(errors="replace"), rc


class DockerProvider(ComputeProvider):
    """ComputeProvider adapter that manages Docker containers.

    Parameters
    ----------
    image:
        Default container image used by ``create()`` when the caller does not
        supply one (default ``"ubuntu:22.04"``).
    docker_bin:
        Path to the Docker CLI binary (default ``"docker"``).
    """

    # Docker snapshots are ``docker commit`` → image tags. There is no *in-place*
    # restore, but :meth:`restore_snapshot` implements an equivalent
    # recreate-from-snapshot-image: stop+remove the existing container and run a
    # fresh one from the snapshot image tag under the same name. Capability
    # callers (e.g. the local-cutover feasibility probe) read this flag to learn
    # the truth rather than inferring it from the mere presence of the method.
    restore_capable: bool = True

    def __init__(
        self,
        image: str = "ubuntu:22.04",
        docker_bin: str = "docker",
        **_kwargs: Any,
    ) -> None:
        self.image: str = image
        self.docker_bin: str = docker_bin

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def create(
        self,
        name: str,
        image: str,
        vcpus: int,
        memory_mb: int,
        disk_gb: int,
        metadata: dict[str, str],
    ) -> str:
        """Run a new detached container. Returns container ID (used as IP)."""
        actual_image = image or self.image
        cmd = [
            self.docker_bin,
            "run",
            "-d",
            "--name", name,
            f"--cpus={vcpus}",
            f"--memory={memory_mb}m",
        ]
        for k, v in metadata.items():
            cmd += ["--label", f"{k}={v}"]
        cmd.append(actual_image)
        # Fleet containers are long-lived compute. Base images like
        # ubuntu:24.04 default to a shell that exits immediately under -d,
        # so pin a keep-alive init command.
        cmd += ["sleep", "infinity"]
        stdout, stderr, rc = await _run(cmd, timeout=300)
        if rc != 0:
            raise RuntimeError(f"docker run failed: {stderr}")
        container_id = stdout.strip()
        logger.debug("Created container %s → %s", name, container_id)
        return container_id

    async def start(self, name: str) -> None:
        _, stderr, rc = await _run([self.docker_bin, "start", name])
        if rc != 0:
            raise RuntimeError(f"docker start failed: {stderr}")

    async def stop(self, name: str, force: bool = False) -> None:
        if force:
            _, stderr, rc = await _run([self.docker_bin, "kill", name])
        else:
            _, stderr, rc = await _run([self.docker_bin, "stop", name])
        if rc != 0:
            raise RuntimeError(f"docker stop/kill failed: {stderr}")

    async def pause(self, name: str) -> None:
        _, stderr, rc = await _run([self.docker_bin, "pause", name])
        if rc != 0:
            raise RuntimeError(f"docker pause failed: {stderr}")

    async def resume(self, name: str) -> None:
        _, stderr, rc = await _run([self.docker_bin, "unpause", name])
        if rc != 0:
            raise RuntimeError(f"docker unpause failed: {stderr}")

    async def restart(self, name: str) -> None:
        _, stderr, rc = await _run([self.docker_bin, "restart", name])
        if rc != 0:
            raise RuntimeError(f"docker restart failed: {stderr}")

    async def delete(self, name: str) -> None:
        _, stderr, rc = await _run([self.docker_bin, "rm", "-f", name])
        if rc != 0:
            raise RuntimeError(f"docker rm failed: {stderr}")

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    async def get_state(self, name: str) -> VMState:
        # Fetch status, IP, and basic resource info via docker inspect.
        stdout, stderr, rc = await _run(
            [
                self.docker_bin,
                "inspect",
                name,
                "--format",
                "{{.State.Status}}\t{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            ]
        )
        if rc != 0:
            return VMState(name=name, status="error")
        parts = stdout.strip().split("\t")
        raw_status = parts[0] if parts else ""
        ip = parts[1] if len(parts) > 1 else None
        return VMState(
            name=name,
            status=_map_docker_status(raw_status),
            ip=ip or None,
            provider_id=name,
        )

    async def get_ip(self, name: str) -> str | None:
        state = await self.get_state(name)
        return state.ip

    async def exists(self, name: str) -> bool:
        _, _, rc = await _run(
            [self.docker_bin, "inspect", name, "--format", "{{.Id}}"]
        )
        return rc == 0

    async def list_vms(self) -> list[str]:
        stdout, _, rc = await _run(
            [self.docker_bin, "ps", "-a", "--format", "{{.Names}}"]
        )
        if rc != 0:
            return []
        return [line.strip() for line in stdout.splitlines() if line.strip()]

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def exec_command(
        self,
        name: str,
        command: list[str],
        timeout: int = 30,
        input_bytes: bytes | None = None,
    ) -> ExecResult:
        cmd_str = " ".join(shlex.quote(c) for c in command)
        docker_cmd = [self.docker_bin, "exec"]
        if input_bytes is not None:
            docker_cmd.append("-i")
        docker_cmd.extend([name, "sh", "-c", cmd_str])
        stdout, stderr, rc = await _run(
            docker_cmd,
            timeout=timeout,
            input_bytes=input_bytes,
        )
        return ExecResult(stdout=stdout, stderr=stderr, exit_code=rc)

    async def exec_interactive(
        self, name: str, command: list[str] | None = None
    ) -> asyncio.subprocess.Process:
        cmd = [self.docker_bin, "exec", "-it", name]
        cmd.extend(command or ["/bin/bash"])
        return await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    async def read_file(self, name: str, path: str) -> bytes:
        result = await self.exec_command(name, ["cat", path])
        if result.exit_code != 0:
            raise FileNotFoundError(f"Cannot read {path}: {result.stderr}")
        return result.stdout.encode()

    async def write_file(self, name: str, path: str, content: bytes) -> None:
        quoted_path = shlex.quote(path)
        result = await self.exec_command(
            name,
            ["sh", "-c", f"cat > {quoted_path}"],
            input_bytes=content,
        )
        if result.exit_code != 0:
            raise RuntimeError(f"writing {path} failed: {result.stderr}")

    async def push_file(self, local_path: str, name: str, remote_path: str) -> None:
        _, stderr, rc = await _run(
            [self.docker_bin, "cp", local_path, f"{name}:{remote_path}"]
        )
        if rc != 0:
            raise RuntimeError(f"docker cp failed: {stderr}")

    async def list_directory(self, name: str, path: str) -> list[DirectoryEntry]:
        result = await self.exec_command(
            name, ["ls", "-la", "--time-style=full-iso", path]
        )
        lines = result.stdout.strip().split("\n")
        entries: list[DirectoryEntry] = []
        for line in lines[1:]:
            parts = line.split(None, 8)
            if len(parts) >= 9:
                entries.append(
                    {
                        "name": parts[-1],
                        "type": "dir" if parts[0].startswith("d") else "file",
                        "size": parts[4],
                        "permissions": parts[0],
                        "modified": f"{parts[5]} {parts[6]}",
                    }
                )
        return entries

    async def delete_file(self, name: str, path: str) -> None:
        await self.exec_command(name, ["rm", "-rf", path])

    # ------------------------------------------------------------------
    # Snapshots (Docker commit-based; returns a SnapshotInfo)
    # ------------------------------------------------------------------

    async def create_snapshot(self, name: str, snapshot_name: str) -> SnapshotInfo:
        """Commit the container to a new image tagged as snapshot_name."""
        _, stderr, rc = await _run(
            [self.docker_bin, "commit", name, snapshot_name]
        )
        if rc != 0:
            raise RuntimeError(f"docker commit failed: {stderr}")
        return SnapshotInfo(
            name=snapshot_name,
            created_at=datetime.now(UTC).isoformat(),
            status="available",
        )

    async def restore_snapshot(self, name: str, snapshot_name: str) -> None:
        """Restore by recreating the container from the snapshot image tag.

        Docker has no in-place snapshot restore: a snapshot is the image tag
        produced by :meth:`create_snapshot` (``docker commit``). This implements
        an equivalent restore by:

        1. capturing the *existing* container's run config (cpus / memory /
           labels) so the recreated container preserves it — best-effort, only
           when a prior container exists;
        2. stopping + removing the existing container, if any (idempotent: a
           missing prior container is fine — this is the first-restore case);
        3. running a fresh detached container under the *same name* from the
           snapshot image tag, mirroring :meth:`create`'s ``docker run`` shape.

        Fail-closed: if the recreate ``docker run`` fails, a ``RuntimeError`` is
        raised with the docker stderr.
        """
        # 1. Capture the prior container's run config (best-effort). When the
        #    container does not exist (first restore), there is nothing to carry
        #    over; the snapshot image itself preserves the committed filesystem.
        run_config = await self._inspect_run_config(name)

        # 2. Stop + remove the existing container if present (idempotent). We use
        #    ``rm -f`` (mirrors :meth:`delete`), which stops then removes in one
        #    step, and tolerate the not-found case so a first restore is a no-op
        #    teardown rather than an error.
        if await self.exists(name):
            _, stderr, rc = await _run([self.docker_bin, "rm", "-f", name])
            if rc != 0:
                raise RuntimeError(
                    f"docker rm failed during restore of {name}: {stderr}"
                )

        # 3. Run a fresh container from the snapshot image under the same name,
        #    mirroring create()'s ``docker run`` invocation (detached, named,
        #    cpus/memory + labels preserved, keep-alive init command).
        cmd = [
            self.docker_bin,
            "run",
            "-d",
            "--name", name,
        ]
        if run_config.get("cpus"):
            cmd.append(f"--cpus={run_config['cpus']}")
        if run_config.get("memory"):
            cmd.append(f"--memory={run_config['memory']}")
        for k, v in run_config.get("labels", {}).items():
            cmd += ["--label", f"{k}={v}"]
        cmd.append(snapshot_name)
        # Pin the same keep-alive init command create() uses so the restored
        # container is long-lived compute rather than exiting immediately.
        cmd += ["sleep", "infinity"]
        _, stderr, rc = await _run(cmd, timeout=300)
        if rc != 0:
            raise RuntimeError(
                f"docker run from snapshot {snapshot_name} failed during "
                f"restore of {name}: {stderr}"
            )
        logger.debug(
            "Restored container %s from snapshot image %s", name, snapshot_name
        )

    async def _inspect_run_config(self, name: str) -> dict[str, Any]:
        """Read an existing container's cpus / memory / labels via inspect.

        Returns an empty-ish dict when the container does not exist (first
        restore) or inspect fails — the snapshot image preserves the filesystem
        state regardless, so a missing prior config is non-fatal.
        """
        stdout, _stderr, rc = await _run(
            [
                self.docker_bin,
                "inspect",
                name,
                "--format",
                "{{.HostConfig.NanoCpus}}\t{{.HostConfig.Memory}}\t"
                "{{json .Config.Labels}}",
            ]
        )
        if rc != 0:
            return {}
        parts = stdout.strip().split("\t")
        config: dict[str, Any] = {}
        # NanoCpus is cpus * 1e9; convert back to the --cpus float form.
        try:
            nano = int(parts[0]) if len(parts) > 0 and parts[0] else 0
            if nano > 0:
                config["cpus"] = nano / 1_000_000_000
        except ValueError:
            pass
        # Memory is bytes; render as the ``<n>m`` form create() uses.
        try:
            mem_bytes = int(parts[1]) if len(parts) > 1 and parts[1] else 0
            if mem_bytes > 0:
                config["memory"] = f"{mem_bytes // (1024 * 1024)}m"
        except ValueError:
            pass
        if len(parts) > 2 and parts[2] and parts[2] != "null":
            import json

            try:
                labels = json.loads(parts[2])
                if isinstance(labels, dict):
                    config["labels"] = {
                        str(k): str(v) for k, v in labels.items()
                    }
            except (ValueError, TypeError):
                pass
        return config

    async def delete_snapshot(self, name: str, snapshot_name: str) -> None:
        _, stderr, rc = await _run([self.docker_bin, "rmi", snapshot_name])
        if rc != 0:
            raise RuntimeError(f"docker rmi failed: {stderr}")

    async def list_snapshots(self, name: str) -> list[SnapshotInfo]:
        # Convention: images whose name starts with "<name>-snapshot" count.
        stdout, _, rc = await _run(
            [self.docker_bin, "images", "--format", "{{.Repository}}:{{.Tag}}", name]
        )
        if rc != 0:
            return []
        return [
            SnapshotInfo(name=line.strip(), created_at="", status="available")
            for line in stdout.splitlines()
            if line.strip()
        ]

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    async def set_config(self, name: str, key: str, value: str) -> None:
        raise NotImplementedError(
            "Docker does not support setting container config at runtime. "
            "Recreate the container with updated parameters."
        )


__all__ = ["DockerProvider"]
