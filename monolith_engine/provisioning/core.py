"""Provisioning core — render a template and provision an agent container.

The engine-side carve of the hosted provisioner's render+run core: load a
template (template.yaml), render its config files (jinja2), create the
container via the provider port, stage configs + provision.sh into it, and
execute the provision script with the NO_SYSTEMD direct-process lane. Secret
resolution goes through the secrets mapping the caller supplies (locally: the
file-backed store / none for local models); the hosted service layers its own
GSM/multi-tenant resolution on top of this same core.
"""

from __future__ import annotations

import base64
import shlex
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

from monolith_engine.ports.provider import ComputeProvider

# Local model providers need a base URL, not a cloud key.
LOCAL_MODEL_PROVIDERS = {"ollama", "endpoint"}
DEFAULT_OLLAMA_BASE_URL = "http://host.docker.internal:11434/v1"


class ProvisionError(RuntimeError):
    def __init__(self, step: str, detail: str):
        self.step = step
        self.detail = detail
        super().__init__(f"[{step}] {detail}")


def templates_dir() -> Path:
    return Path(str(resources.files("monolith_engine"))) / "templates"


@dataclass
class TemplateSpec:
    name: str
    raw: dict
    path: Path

    @property
    def defaults(self) -> dict:
        return dict(self.raw.get("defaults") or {})

    @property
    def docker_image(self) -> str:
        providers = self.raw.get("providers") or {}
        docker = providers.get("docker") or {}
        return str(docker.get("image") or "ubuntu:24.04")

    @property
    def visibility(self) -> str:
        return str(self.raw.get("visibility") or "public")


def load_template(name: str) -> TemplateSpec:
    path = templates_dir() / name / "template.yaml"
    if not path.is_file():
        raise ProvisionError("template", f"template '{name}' not found")
    return TemplateSpec(name=name, raw=yaml.safe_load(path.read_text()) or {}, path=path.parent)


def list_templates() -> list[dict]:
    out = []
    for tdir in sorted(templates_dir().iterdir()):
        ty = tdir / "template.yaml"
        if not ty.is_file():
            continue
        raw = yaml.safe_load(ty.read_text()) or {}
        out.append(
            {
                "name": raw.get("name", tdir.name),
                "display_name": raw.get("display_name", tdir.name),
                "description": raw.get("description", ""),
                "visibility": raw.get("visibility", "public"),
            }
        )
    return out


def _env_file_key(value: object) -> str:
    return str(value).strip().upper().replace("-", "_")


def _env_file_value(value: object) -> str:
    return str(value)


def render_configs(template: TemplateSpec, context: dict) -> dict[str, str]:
    configs_dir = template.path / "configs"
    if not configs_dir.exists():
        return {}
    env = Environment(loader=FileSystemLoader(str(configs_dir)), keep_trailing_newline=True)  # nosec B701
    env.filters["env_key"] = _env_file_key
    env.filters["env_value"] = _env_file_value
    rendered = {}
    for j2_file in sorted(configs_dir.glob("*.j2")):
        rendered[j2_file.stem] = env.get_template(j2_file.name).render(**context)
    return rendered


def build_context(template: TemplateSpec, *, agent_name: str, role: str, model: str | None,
                  model_provider: str | None, model_base_url: str | None,
                  gateway_port: str | None = None, secrets: dict[str, str] | None = None) -> dict:
    defaults = template.defaults
    provider = (model_provider or defaults.get("model_provider") or "openrouter").strip().lower()
    base_url = model_base_url or (
        DEFAULT_OLLAMA_BASE_URL if provider == "ollama" else defaults.get("model_base_url")
    )
    ctx = {
        "agent_name": agent_name,
        "agent_slug": agent_name,
        "agent_role": role,
        "model": model or defaults.get("model", ""),
        "model_provider": provider,
        "model_base_url": base_url,
        "timezone": defaults.get("timezone", "UTC"),
        "personality": defaults.get("personality", "professional"),
        "secrets_mode": "env",
        "gateway_port": gateway_port or defaults.get("gateway_port", "18789"),
        "secrets": [
            {"env_var": k, "value": v} for k, v in (secrets or {}).items() if v
        ],
    }
    return ctx


async def _push_text(provider: ComputeProvider, container: str, content: str, remote_path: str) -> None:
    # base64 through exec — works on every provider without host temp files.
    b64 = base64.b64encode(content.encode()).decode()
    quoted = shlex.quote(remote_path)
    result = await provider.exec_command(
        container, ["sh", "-c", f"echo '{b64}' | base64 -d > {quoted}"], timeout=60
    )
    if result.exit_code != 0:
        raise ProvisionError("push", f"writing {remote_path} failed: {result.stderr.strip()}")


async def provision_agent(
    provider: ComputeProvider,
    *,
    template_name: str,
    agent_name: str,
    role: str = "agent",
    model: str | None = None,
    model_provider: str | None = None,
    model_base_url: str | None = None,
    secrets: dict[str, str] | None = None,
    vcpus: int = 2,
    memory_mb: int = 2048,
    provision_timeout: int = 600,
) -> dict:
    """Provision one agent container from a template, end to end.

    Returns {id, image, status, template}. Raises ProvisionError on failure.
    """
    template = load_template(template_name)
    image = template.docker_image
    context = build_context(
        template,
        agent_name=agent_name,
        role=role,
        model=model,
        model_provider=model_provider,
        model_base_url=model_base_url,
        secrets=secrets,
    )

    # 1. Create the container (keep-alive init; labels mark engine management).
    try:
        await provider.create(
            agent_name, image, vcpus, memory_mb, 10,
            {"com.thisismonolith.engine": "1", "com.thisismonolith.template": template_name},
        )
    except RuntimeError as exc:
        raise ProvisionError("create", str(exc)) from exc

    # 2. Render + stage configs and the provision script.
    rendered = render_configs(template, context)
    await provider.exec_command(agent_name, ["mkdir", "-p", "/tmp/raava-provision"], timeout=30)
    for filename, content in rendered.items():
        await _push_text(provider, agent_name, content, f"/tmp/raava-provision/{filename}")
    script = (template.path / "provision.sh").read_text()
    await _push_text(provider, agent_name, script, "/tmp/raava-provision.sh")

    # 3. Run the provision script on the NO_SYSTEMD lane.
    env_prefix = " ".join(
        f"{k}={shlex.quote(v)}"
        for k, v in {
            "NO_SYSTEMD": "1",
            "SECRETS_MODE": str(context["secrets_mode"]),
            "GATEWAY_PORT": str(context["gateway_port"]),
        }.items()
    )
    result = await provider.exec_command(
        agent_name,
        ["bash", "-c", f"{env_prefix} bash /tmp/raava-provision.sh {shlex.quote(agent_name)}"],
        timeout=provision_timeout,
    )
    if result.exit_code != 0:
        raise ProvisionError(
            "provision_script",
            f"exit {result.exit_code}: {(result.stderr or result.stdout or '')[-1500:]}",
        )

    state = await provider.get_state(agent_name)
    return {"id": agent_name, "image": image, "status": state.status, "ip": state.ip, "template": template_name}


__all__ = [
    "ProvisionError",
    "TemplateSpec",
    "build_context",
    "list_templates",
    "load_template",
    "provision_agent",
    "render_configs",
]
