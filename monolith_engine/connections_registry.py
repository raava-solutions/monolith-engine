"""Connector registry — the available external-source connector types (v1).

Each connector declares its config fields and which are secrets. Secret fields
MUST be provided as ``env:VAR`` references so raw credentials never enter the
connection record (the vault/bridge resolves the handle at use time). This
mirrors ``fleet/observability/registry.py`` (a connector + a registry lookup).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A secret field must be a concrete env: reference (env:VAR_NAME), not a loose
# prefix that could smuggle an empty or malformed value.
_ENV_REF_RE = re.compile(r"^env:[A-Z_][A-Z0-9_]*$")


class ConnectorError(ValueError):
    """Raised for an unknown connector or invalid connection config."""

    def __init__(self, message: str, *, code: str = "invalid_connection") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ConnectorField:
    name: str
    secret: bool = False
    required: bool = True


@dataclass(frozen=True)
class Connector:
    type: str
    fields: tuple[ConnectorField, ...]

    def scope_vocabulary(self) -> str:
        """The envelope scope token a step needs to use this connector (post-v1)."""
        return f"connection:{self.type}"

    def validate_config(self, config: dict) -> None:
        """Validate a connection config: required fields present, secrets are refs."""
        for field in self.fields:
            value = config.get(field.name)
            if value is None:
                if field.required:
                    raise ConnectorError(
                        f"connector '{self.type}' requires field '{field.name}'",
                        code="missing_field",
                    )
                continue
            if field.secret and not (
                isinstance(value, str) and _ENV_REF_RE.match(value)
            ):
                raise ConnectorError(
                    f"field '{field.name}' must be an 'env:VAR' reference (e.g. "
                    "'env:GITHUB_TOKEN'), not an inline secret — keep credentials "
                    "out of the connection",
                    code="inline_secret",
                )
        unknown = set(config) - {f.name for f in self.fields}
        if unknown:
            raise ConnectorError(
                f"connector '{self.type}' has no field(s): {', '.join(sorted(unknown))}",
                code="unknown_field",
            )


_CONNECTORS: dict[str, Connector] = {
    "github": Connector(
        type="github",
        fields=(ConnectorField("token", secret=True),),
    ),
    "slack": Connector(
        type="slack",
        fields=(ConnectorField("bot_token", secret=True),),
    ),
    "postgres": Connector(
        type="postgres",
        fields=(ConnectorField("dsn", secret=True),),
    ),
    "http_bearer": Connector(
        type="http_bearer",
        fields=(
            ConnectorField("endpoint", secret=False),
            ConnectorField("token", secret=True),
        ),
    ),
}


def get_connector(connector_type: str) -> Connector:
    connector = _CONNECTORS.get(connector_type)
    if connector is None:
        raise ConnectorError(
            f"unknown connector type '{connector_type}'. Known: "
            f"{', '.join(sorted(_CONNECTORS))}",
            code="unknown_connector",
        )
    return connector


def known_connector_types() -> list[str]:
    return sorted(_CONNECTORS)


__all__ = [
    "Connector",
    "ConnectorError",
    "ConnectorField",
    "get_connector",
    "known_connector_types",
]
