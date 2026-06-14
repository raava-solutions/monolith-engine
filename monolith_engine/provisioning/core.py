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
import os
import shlex
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

from monolith_engine.ports.provider import ComputeProvider

# Local model providers need a base URL, not a cloud key.
LOCAL_MODEL_PROVIDERS = {"ollama", "endpoint"}
DEFAULT_OLLAMA_BASE_URL = "http://host.docker.internal:11434/v1"
DEFAULT_USER_TEMPLATE_DIR = Path("~/.config/monolith/templates")


class ProvisionError(RuntimeError):
    def __init__(self, step: str, detail: str):
        self.step = step
        self.detail = detail
        super().__init__(f"[{step}] {detail}")


def templates_dir() -> Path:
    return Path(str(resources.files("monolith_engine"))) / "templates"


def user_templates_dirs() -> list[Path]:
    configured = os.environ.get("MONOLITH_TEMPLATE_DIR")
    raw_dirs = configured.split(os.pathsep) if configured else [str(DEFAULT_USER_TEMPLATE_DIR)]
    return [Path(raw).expanduser() for raw in raw_dirs if raw]


def template_dirs() -> list[Path]:
    return [*user_templates_dirs(), templates_dir()]


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


@dataclass
class LocalStage:
    source: Path
    target: str
    required: bool = True


def load_template(name: str) -> TemplateSpec:
    # Template names must be a single, literal path component. Reject separators,
    # empty/relative names, and anything Path would split, so a user template dir
    # can never be coaxed into resolving outside its own root.
    if "/" in name or "\\" in name or name in ("", ".", "..") or Path(name).parts != (name,):
        raise ProvisionError("template", f"template '{name}' not found")
    for root in template_dirs():
        path = root / name / "template.yaml"
        if path.is_file():
            return TemplateSpec(name=name, raw=yaml.safe_load(path.read_text()) or {}, path=path.parent)
    raise ProvisionError("template", f"template '{name}' not found")


def list_templates() -> list[dict]:
    out = []
    seen = set()
    for root in template_dirs():
        if not root.is_dir():
            continue
        for tdir in sorted(root.iterdir()):
            ty = tdir / "template.yaml"
            if not ty.is_file() or tdir.name in seen:
                continue
            raw = yaml.safe_load(ty.read_text()) or {}
            seen.add(tdir.name)
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
    return shlex.quote(str(value))


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
                  gateway_port: str | None = None, secrets: dict[str, str] | None = None,
                  trace_enabled: bool = False, tenant_id: str = "local") -> dict:
    defaults = template.defaults
    provider = (model_provider or defaults.get("model_provider") or "openrouter").strip().lower()
    base_url = model_base_url or (
        DEFAULT_OLLAMA_BASE_URL if provider == "ollama" else defaults.get("model_base_url")
    )
    secret_values = {k: v for k, v in (secrets or {}).items() if v}
    container_name = agent_name
    ctx = {
        "agent_name": agent_name,
        "agent_slug": agent_name,
        "container_name": container_name,
        "agent_role": role,
        "model": model or defaults.get("model", ""),
        "model_provider": provider,
        "model_base_url": base_url,
        "timezone": defaults.get("timezone", "UTC"),
        "personality": defaults.get("personality", "professional"),
        "secrets_mode": "env",
        "gateway_port": gateway_port or defaults.get("gateway_port", "18789"),
        "trace_enabled": trace_enabled,
        "tenant_id": tenant_id,
        "otel_service_name": defaults.get("otel_service_name", container_name),
        "otel_exporter_otlp_endpoint": defaults.get("otel_exporter_otlp_endpoint"),
        "otel_resource_attributes": defaults.get(
            "otel_resource_attributes",
            f"tenant_id={tenant_id},agent_name={agent_name},container_id={container_name}",
        ),
        "secrets_dict": secret_values,
        "secrets": [
            {"env_var": k, "value": v} for k, v in secret_values.items()
        ],
    }
    return ctx


def _is_local_docker_provider(provider: ComputeProvider) -> bool:
    cls = provider.__class__
    return cls.__name__ == "DockerProvider" or cls.__module__.endswith(".docker")


def _iter_local_stage_entries(template: TemplateSpec) -> list[dict]:
    entries: list[dict] = []
    raw_entries = template.raw.get("local_stages") or []
    if isinstance(raw_entries, list):
        entries.extend(item for item in raw_entries if isinstance(item, dict))
    docker = (template.raw.get("providers") or {}).get("docker") or {}
    docker_entries = docker.get("local_stages") or []
    if isinstance(docker_entries, list):
        entries.extend(item for item in docker_entries if isinstance(item, dict))
    return entries


def _template_local_stages(template: TemplateSpec) -> list[LocalStage]:
    stages: list[LocalStage] = []
    for entry in _iter_local_stage_entries(template):
        source = str(entry.get("source") or entry.get("local_path") or "").strip()
        target = str(entry.get("target") or entry.get("remote_path") or "").strip()
        if not source or not target.startswith("/"):
            continue
        stages.append(
            LocalStage(
                source=Path(source).expanduser(),
                target=target,
                required=bool(entry.get("required", True)),
            )
        )

    script_path = template.path / "provision.sh"
    try:
        script = script_path.read_text()
    except OSError:
        script = ""
    outreach_source = Path(os.environ.get("RAAVA_OUTREACH_PATH", "/Users/master/raava-outreach")).expanduser()
    if "/opt/raava-outreach" in script:
        stages.append(LocalStage(source=outreach_source, target="/opt/raava-outreach", required=False))

    deduped: dict[str, LocalStage] = {}
    for stage in stages:
        deduped[stage.target] = stage
    return list(deduped.values())


async def _stage_local_paths(provider: ComputeProvider, container: str, template: TemplateSpec) -> None:
    if not _is_local_docker_provider(provider):
        return
    for stage in _template_local_stages(template):
        if not stage.source.exists():
            if stage.required:
                raise ProvisionError("stage", f"local stage source not found: {stage.source}")
            continue
        parent = shlex.quote(str(Path(stage.target).parent))
        result = await provider.exec_command(container, ["sh", "-c", f"mkdir -p {parent}"], timeout=30)
        if result.exit_code != 0:
            raise ProvisionError("stage", f"creating {Path(stage.target).parent} failed: {result.stderr.strip()}")
        try:
            await provider.push_file(str(stage.source), container, stage.target)
        except Exception as exc:
            raise ProvisionError("stage", f"staging {stage.source} to {stage.target} failed: {exc}") from exc


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
    await _stage_local_paths(provider, agent_name, template)
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
    "LocalStage",
    "TemplateSpec",
    "build_context",
    "list_templates",
    "load_template",
    "provision_agent",
    "render_configs",
]
