"""Provisioning env file value quoting."""

from __future__ import annotations

import shlex
import subprocess

from monolith_engine.provisioning.core import _env_file_value


def _source_env_value(tmp_path, value: str) -> str:
    env_file = tmp_path / "agent.env"
    env_file.write_text(f"KEY={_env_file_value(value)}\n")
    result = subprocess.run(
        ["sh", "-c", f". {shlex.quote(str(env_file))}; printf %s \"$KEY\""],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_env_file_value_leaves_simple_values_unquoted():
    values = [
        "hermes_alpha",
        "https://example.test/api/v1",
        "/home/agent/.hermes,/home/agent/.config/hermes,/home/agent/workspace",
    ]

    for value in values:
        assert _env_file_value(value) == value


def test_env_file_value_shell_quotes_health_command(tmp_path):
    value = "systemctl is-active --quiet hermes-gateway && test -d /home/agent/.hermes"

    assert _source_env_value(tmp_path, value) == value


def test_env_file_value_shell_quotes_single_quote(tmp_path):
    value = "agent's runtime"

    assert _source_env_value(tmp_path, value) == value
