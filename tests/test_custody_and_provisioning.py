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
    render_configs,
)


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


# ── Provisioning core (render path; container exec covered live) ─────────────


def test_templates_ship_in_package():
    names = {t["name"] for t in list_templates()}
    assert {"hermes", "hermes-direct", "openclaw"} <= names


def test_unknown_template_raises():
    with pytest.raises(ProvisionError):
        load_template("ghost")


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
