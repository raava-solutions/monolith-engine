#!/bin/bash
set -euo pipefail

AGENT_SLUG="${1:?Usage: provision.sh <agent_slug>}"

ensure_python_venv() {
    python3 - <<'PY'
import shutil
import subprocess
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp(prefix="raava-venv-check-"))
venv_dir = tmp / "venv"
try:
    subprocess.run(["python3", "-m", "venv", str(venv_dir)], check=True)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
PY
}

echo "[argus] Ensuring required packages are installed..."
apt-get update -qq
apt-get install -y -qq git python3-venv 2>&1 | tail -3

SECRETS_MODE="${SECRETS_MODE:-env}"
HERMES_REF="${HERMES_REF:-raava-fleet-operator-v1}"
INSTALL_EXTRAS="${INSTALL_EXTRAS:-messaging}"

echo "[argus] Provisioning Argus fleet operator: $AGENT_SLUG"
echo "[argus] secrets_mode=$SECRETS_MODE hermes_ref=$HERMES_REF install_extras=$INSTALL_EXTRAS"

for i in $(seq 1 15); do
    if getent hosts github.com &>/dev/null; then
        break
    fi
    echo "[argus] Waiting for DNS... ($i/15)"
    sleep 2
done

if [ ! -d /home/agent/hermes-agent ]; then
    echo "[argus] Installing Raava Hermes fork..."
    cd /home/agent
    git clone https://github.com/raava-solutions/hermes-agent.git hermes-agent
    cd hermes-agent
    git checkout "$HERMES_REF"
    ensure_python_venv
    python3 -m venv venv
    source venv/bin/activate
    pip install -e ".[$INSTALL_EXTRAS]" -q
    chown -R agent:agent /home/agent/hermes-agent
else
    echo "[argus] Hermes already installed"
fi

echo "[argus] Installing configs..."
mkdir -p /home/agent/.hermes /etc/hermes

cp /tmp/raava-provision/config.yaml /home/agent/.hermes/config.yaml
cp /tmp/raava-provision/SOUL.md /home/agent/.hermes/SOUL.md
cp /tmp/raava-provision/fleet-operator.yaml /home/agent/.hermes/fleet-operator.yaml
chown agent:agent /home/agent/.hermes/config.yaml /home/agent/.hermes/SOUL.md /home/agent/.hermes/fleet-operator.yaml
chmod 600 /home/agent/.hermes/config.yaml /home/agent/.hermes/fleet-operator.yaml

cp /tmp/raava-provision/hermes-gateway.service /etc/systemd/system/hermes-gateway.service
chmod 644 /etc/systemd/system/hermes-gateway.service

cp /tmp/raava-provision/env "/etc/hermes/${AGENT_SLUG}.env"
chown root:root "/etc/hermes/${AGENT_SLUG}.env"
chmod 600 "/etc/hermes/${AGENT_SLUG}.env"

if [ -f /tmp/raava-provision/monolith-fleet.sh ]; then
    cp /tmp/raava-provision/monolith-fleet.sh /usr/local/bin/monolith-fleet
    chmod 755 /usr/local/bin/monolith-fleet
fi

MCP_DIR="/home/agent/.hermes/mcp-servers"
mkdir -p "$MCP_DIR"
cp /tmp/raava-provision/monolith-fleet.json "$MCP_DIR/monolith-fleet.json"
chown -R agent:agent "$MCP_DIR"
chmod 600 "$MCP_DIR/monolith-fleet.json"

SKILLS_DIR="/home/agent/.hermes/skills"
mkdir -p "$SKILLS_DIR"
for skill_src in /tmp/raava-provision/skills-*.md; do
    [ -f "$skill_src" ] || continue
    skill_file="$(basename "$skill_src")"
    skill_name="${skill_file#skills-}"
    cp "$skill_src" "$SKILLS_DIR/${skill_name}"
    chown agent:agent "$SKILLS_DIR/${skill_name}"
    echo "[argus] Skill installed: ${skill_name%.md}"
done

systemctl daemon-reload
systemctl enable hermes-gateway
systemctl start hermes-gateway

echo "[argus] Verifying gateway..."
for i in $(seq 1 15); do
    if systemctl is-active --quiet hermes-gateway; then
        break
    fi
    sleep 2
done

if ! systemctl is-active --quiet hermes-gateway; then
    echo "[argus] WARNING: Gateway failed to start"
    journalctl -u hermes-gateway -n 20 --no-pager
    exit 1
fi

sleep 5
if systemctl is-active --quiet hermes-gateway; then
    PID=$(pgrep -f "hermes_cli.main gateway" -o 2>/dev/null || echo "unknown")
    echo "[argus] Agent verified stable (PID: $PID)"
else
    echo "[argus] WARNING: Gateway started but crashed within 5s"
    journalctl -u hermes-gateway -n 20 --no-pager
    exit 1
fi

echo "[argus] Provisioning complete"
