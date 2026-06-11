"""Pydantic models + validation for file-first workflow definitions.

A workflow definition is authored as TOML and applied to the Fleet API. The
raw TOML body is stored byte-for-byte (round-trips exactly), while the parsed
``WorkflowSpec`` is what the engine validates and executes.

Secret discipline mirrors the stack spec (the stack spec): secrets
are declared as ``env:VAR`` references only — inline plaintext is rejected at
validation so a definition can be committed to a repo without leaking
credentials.
"""

from __future__ import annotations

import re
import tomllib

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

CONCURRENCY_POLICIES = ("allow", "singleton", "queue")
ON_FAILURE_POLICIES = ("fail_run", "continue")
STEP_TYPES = ("deterministic", "agent")

# An env: secret reference must name a concrete environment variable —
# ``env:`` alone, ``env: `` (trailing space), or ``env:lowercase`` are rejected
# so a loose prefix can't smuggle an empty or malformed reference.
ENV_REF_RE = re.compile(r"^env:[A-Z_][A-Z0-9_]*$")


def is_env_ref(value: object) -> bool:
    return isinstance(value, str) and ENV_REF_RE.match(value) is not None


class WorkflowSpecError(ValueError):
    """Raised when a workflow TOML body is malformed or fails validation.

    Carries a stable ``code`` so the router can surface a typed error without
    leaking internals.
    """

    def __init__(self, message: str, *, code: str = "invalid_workflow") -> None:
        super().__init__(message)
        self.code = code


class StepSpec(BaseModel):
    name: str
    type: str
    run: str | None = None  # shell command for deterministic steps
    prompt: str | None = None  # bounded task for agent steps
    target: str | None = None  # container the step executes against (overrides workflow target)
    condition: str | None = None  # e.g. "steps.build.status == succeeded"
    retry_budget: int = Field(default=0, ge=0, le=10)
    on_failure: str = "fail_run"

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v not in STEP_TYPES:
            raise ValueError(
                f"step type '{v}' is not one of {', '.join(STEP_TYPES)}"
            )
        return v

    @field_validator("on_failure")
    @classmethod
    def _known_on_failure(cls, v: str) -> str:
        if v not in ON_FAILURE_POLICIES:
            raise ValueError(
                f"on_failure '{v}' is not one of {', '.join(ON_FAILURE_POLICIES)}"
            )
        return v

    @model_validator(mode="after")
    def _require_action_for_type(self) -> StepSpec:
        if self.type == "deterministic" and not self.run:
            raise ValueError(
                f"deterministic step '{self.name}' requires a 'run' command"
            )
        if self.type == "agent" and not self.prompt:
            raise ValueError(f"agent step '{self.name}' requires a 'prompt'")
        return self


class WorkflowSpec(BaseModel):
    name: str
    concurrency: str = "singleton"
    target: str | None = None  # default container for steps that name none
    secrets: dict[str, str] = {}
    steps: list[StepSpec]

    @field_validator("concurrency")
    @classmethod
    def _known_concurrency(cls, v: str) -> str:
        if v not in CONCURRENCY_POLICIES:
            raise ValueError(
                f"concurrency '{v}' is not one of {', '.join(CONCURRENCY_POLICIES)}"
            )
        return v

    @field_validator("secrets")
    @classmethod
    def _secrets_are_env_refs(cls, v: dict[str, str]) -> dict[str, str]:
        for key, value in v.items():
            if not is_env_ref(value):
                raise ValueError(
                    f"secret '{key}' must be an 'env:VAR' reference (e.g. "
                    "'env:GITHUB_TOKEN'), not an inline value — keep credentials "
                    "out of the definition"
                )
        return v

    @field_validator("steps")
    @classmethod
    def _steps_present_and_named(cls, v: list[StepSpec]) -> list[StepSpec]:
        if not v:
            raise ValueError("workflow must declare at least one step")
        names = [s.name for s in v]
        if len(names) != len(set(names)):
            raise ValueError("step names must be unique within a workflow")
        return v


def parse_workflow_toml(body: str) -> WorkflowSpec:
    """Parse and validate a raw TOML workflow body into a ``WorkflowSpec``.

    Raises ``WorkflowSpecError`` (with a stable ``code``) on malformed TOML or
    schema-validation failure so the router maps it to a typed 400.
    """
    try:
        data = tomllib.loads(body)
    except tomllib.TOMLDecodeError as exc:
        raise WorkflowSpecError(
            f"workflow body is not valid TOML: {exc}", code="malformed_toml"
        ) from exc
    try:
        return WorkflowSpec.model_validate(data)
    except ValidationError as exc:
        # Surface the first error message; keep it operator-readable.
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        msg = first.get("msg", "validation error")
        raise WorkflowSpecError(
            f"{loc}: {msg}" if loc else msg, code="invalid_workflow"
        ) from exc
