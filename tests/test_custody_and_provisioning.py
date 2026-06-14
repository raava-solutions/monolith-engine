"""U6 — local custody keys + provisioning core (template render path)."""

from __future__ import annotations

import pytest

from monolith_engine.custody.local_key import (
    ensure_local_key,
    mint_run_token,
    verify_run_token,
)
from monolith_engine.provisioning.core import (
    ProvisionError,
    build_context,
    list_templates,
    load_template,
    provision_agent,
    render_configs,
)


@pytest.fixture(autouse=True)
def isolated_template_dirs(monkeypatch, tmp_path):
    monkeypatch.setenv("MONOLITH_TEMPLATE_DIR", str(tmp_path / "templates"))


def test_local_key_generated_0600_and_reused(tmp_path):
    p = tmp_path / "keys" / "signing.key"
    pem1 = ensure_local_key(p)
    assert p.stat().st_mode & 0o077 == 0
    pem2 = ensure_local_key(p)
    assert pem1 == pem2


def test_loose_keystore_refused(tmp_path):
    p = tmp_path / "signing.key"
    ensure_local_key(p)
    p.chmod(0o644)
    with pytest.raises(PermissionError):
        ensure_local_key(p)


def test_mint_verify_roundtrip(tmp_path):
    pem = ensure_local_key(tmp_path / "k")
    token = mint_run_token(pem, tenant_id="local", container_id="agent-1", scopes=["chat"])
    claims = verify_run_token(token, pem)
    assert claims["tenant"] == "local" and claims["sub"] == "agent-1"


def test_tampered_token_refused(tmp_path):
    import jwt as _jwt

    pem = ensure_local_key(tmp_path / "k")
    token = mint_run_token(pem, tenant_id="local", container_id="agent-1")
    bad = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(_jwt.InvalidTokenError):
        verify_run_token(bad, pem)


class DockerProvider:
    def __init__(self):
        self.containers: dict[str, dict] = {}
        self.exec_calls: list[list[str]] = []
        self.pushes: list[tuple[str, str, str]] = []

    async def create(self, name, image, vcpus, memory_mb, disk_gb, metadata):
        self.containers[name] = {"image": image, "status": "running", "ip": "172.17.0.2"}
        return "container-id"

    async def exec_command(self, name, command, timeout=30):
        from monolith_engine.ports.provider import ExecResult

        self.exec_calls.append(command)
        return ExecResult(stdout="ok\n", stderr="", exit_code=0)

    async def get_state(self, name):
        from monolith_engine.ports.provider import VMState

        c = self.containers[name]
        return VMState(name=name, status=c["status"], ip=c["ip"])

    async def push_file(self, local_path, name, remote_path):
        self.pushes.append((local_path, name, remote_path))


# ── Provisioning core (render path; container exec covered live) ─────────────


def test_templates_ship_in_package():
    names = {t["name"] for t in list_templates()}
    assert {"hermes", "hermes-direct", "openclaw"} <= names


def test_unknown_template_raises():
    with pytest.raises(ProvisionError):
        load_template("ghost")


def test_user_template_dir_adds_private_template(monkeypatch, tmp_path):
    user_templates = tmp_path / "user-templates"
    template_dir = user_templates / "private"
    template_dir.mkdir(parents=True)
    (template_dir / "template.yaml").write_text(
        "name: private\n"
        "display_name: Private Template\n"
        "providers:\n"
        "  docker:\n"
        "    image: private:latest\n"
    )
    monkeypatch.setenv("MONOLITH_TEMPLATE_DIR", str(user_templates))

    assert load_template("private").docker_image == "private:latest"


def test_user_template_dir_overrides_packaged_template(monkeypatch, tmp_path):
    user_templates = tmp_path / "user-templates"
    template_dir = user_templates / "hermes-direct"
    template_dir.mkdir(parents=True)
    (template_dir / "template.yaml").write_text(
        "name: hermes-direct\n"
        "display_name: Local Override\n"
        "providers:\n"
        "  docker:\n"
        "    image: local-hermes-direct:latest\n"
    )
    monkeypatch.setenv("MONOLITH_TEMPLATE_DIR", str(user_templates))

    assert load_template("hermes-direct").docker_image == "local-hermes-direct:latest"


def test_packaged_template_fallback_when_user_dir_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("MONOLITH_TEMPLATE_DIR", str(tmp_path / "missing"))

    assert load_template("hermes-direct").docker_image == "monolith-hermes-golden:latest"


def test_hermes_direct_renders_local_model_as_custom():
    t = load_template("hermes-direct")
    ctx = build_context(
        t, agent_name="a1", role="agent", model="glm-4.7-flash",
        model_provider="ollama", model_base_url=None,
    )
    rendered = render_configs(t, ctx)
    assert "config.yaml" in rendered
    assert "provider: custom" in rendered["config.yaml"]
    assert "OPENAI_BASE_URL=http://host.docker.internal:11434/v1" in rendered["env"]


def test_docker_image_resolves_golden():
    assert load_template("hermes-direct").docker_image == "monolith-hermes-golden:latest"
    assert load_template("openclaw").docker_image == "monolith-openclaw-golden:latest"


def test_build_context_emits_observability_context():
    t = load_template("hermes")
    ctx = build_context(
        t, agent_name="gtm-local", role="agent", model=None,
        model_provider="openrouter", model_base_url=None,
        trace_enabled=True, tenant_id="tenant-123",
    )

    assert ctx["trace_enabled"] is True
    assert ctx["tenant_id"] == "tenant-123"
    assert ctx["container_name"] == "gtm-local"
    assert ctx["otel_service_name"] == "gtm-local"
    assert ctx["otel_resource_attributes"] == (
        "tenant_id=tenant-123,agent_name=gtm-local,container_id=gtm-local"
    )


def test_hermes_env_renders_langfuse_when_trace_enabled():
    t = load_template("hermes")
    ctx = build_context(
        t, agent_name="gtm-local", role="agent", model=None,
        model_provider="openrouter", model_base_url=None,
        trace_enabled=True, tenant_id="tenant-123",
        secrets={
            "OPENROUTER_API_KEY": "sk-or",
            "HERMES_LANGFUSE_PUBLIC_KEY": "pk-lf",
            "HERMES_LANGFUSE_SECRET_KEY": "sk-lf",
            "HERMES_LANGFUSE_HOST": "https://langfuse.example.test",
        },
    )
    rendered = render_configs(t, ctx)

    assert "MONOLITH_TRACE_ENABLED=true" in rendered["env"]
    assert "HERMES_LANGFUSE_PUBLIC_KEY=pk-lf" in rendered["env"]
    assert "HERMES_LANGFUSE_SECRET_KEY=sk-lf" in rendered["env"]
    assert "HERMES_LANGFUSE_HOST=https://langfuse.example.test" in rendered["env"]
    assert "OTEL_SERVICE_NAME=gtm-local" in rendered["env"]


def test_hermes_no_systemd_preserves_langfuse_and_otel_env():
    script = (load_template("hermes").path / "provision.sh").read_text()

    assert "HERMES_LANGFUSE_PUBLIC_KEY" in script
    assert "HERMES_LANGFUSE_SECRET_KEY" in script
    assert "HERMES_LANGFUSE_HOST" in script
    assert "OTEL_SERVICE_NAME" in script
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in script
    assert "OTEL_RESOURCE_ATTRIBUTES" in script


def test_provision_agent_stages_template_local_paths_for_docker(monkeypatch, tmp_path):
    import asyncio

    source = tmp_path / "source-repo"
    source.mkdir()
    user_templates = tmp_path / "user-templates"
    template_dir = user_templates / "private"
    template_dir.mkdir(parents=True)
    (template_dir / "template.yaml").write_text(
        "name: private\n"
        "providers:\n"
        "  docker:\n"
        "    image: private:latest\n"
        "    local_stages:\n"
        f"      - source: {source}\n"
        "        target: /opt/private-source\n"
    )
    (template_dir / "provision.sh").write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("MONOLITH_TEMPLATE_DIR", str(user_templates))

    provider = DockerProvider()
    asyncio.run(
        provision_agent(
            provider,
            template_name="private",
            agent_name="agent-local-stage",
            role="agent",
        )
    )

    assert provider.pushes == [(str(source), "agent-local-stage", "/opt/private-source")]
    assert ["sh", "-c", "mkdir -p /opt"] in provider.exec_calls
