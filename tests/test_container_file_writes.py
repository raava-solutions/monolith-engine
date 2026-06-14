"""Regression coverage for container file writes using stdin."""

from __future__ import annotations

from monolith_engine.ports.provider import ExecResult
from monolith_engine.providers import docker as docker_module
from monolith_engine.providers.docker import DockerProvider
from monolith_engine.provisioning.core import _push_text


async def test_docker_write_file_streams_content_over_stdin(monkeypatch):
    captured = {}
    payload = b"large-argv-regression-payload" * 1024

    async def fake_run(args, timeout=30, input_bytes=None):
        captured["args"] = args
        captured["timeout"] = timeout
        captured["input_bytes"] = input_bytes
        return "", "", 0

    monkeypatch.setattr(docker_module, "_run", fake_run)

    provider = DockerProvider()
    await provider.write_file("agent-1", "/tmp/payload.txt", payload)

    argv = " ".join(captured["args"])
    assert captured["input_bytes"] == payload
    assert captured["args"][:3] == ["docker", "exec", "-i"]
    assert payload.decode() not in argv
    assert "cat > /tmp/payload.txt" in argv


async def test_push_text_streams_content_over_stdin():
    payload = "large-provisioning-template" * 1024

    class CapturingProvider:
        def __init__(self):
            self.command = None
            self.input_bytes = None
            self.timeout = None

        async def exec_command(self, name, command, timeout=30, input_bytes=None):
            self.command = command
            self.timeout = timeout
            self.input_bytes = input_bytes
            return ExecResult(stdout="", stderr="", exit_code=0)

    provider = CapturingProvider()
    await _push_text(provider, "agent-1", payload, "/tmp/rendered-config.py")

    argv = " ".join(provider.command)
    assert provider.input_bytes == payload.encode()
    assert provider.timeout == 60
    assert payload not in argv
    assert "cat > /tmp/rendered-config.py" in argv
