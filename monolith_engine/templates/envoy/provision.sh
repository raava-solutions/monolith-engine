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

retry_apt() {
    local attempts=0
    until "$@"; do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge 30 ]; then
            echo "[raava] apt command failed after ${attempts} attempts: $*" >&2
            return 1
        fi
        echo "[raava] apt busy or failed; retrying in 5s (${attempts}/30): $*" >&2
        sleep 5
    done
}

SECRETS_MODE="${SECRETS_MODE:-env}"
HERMES_REF="${HERMES_REF:-498bfc7bc12a937621b4215312049b1000726df3}"
INSTALL_EXTRAS="${INSTALL_EXTRAS:-messaging}"

echo "[raava] Provisioning Envoy Paperclip FDE: $AGENT_SLUG"
echo "[raava] secrets_mode=$SECRETS_MODE hermes_ref=$HERMES_REF install_extras=$INSTALL_EXTRAS"

retry_apt apt-get update -qq
retry_apt apt-get install -y -qq git python3-venv sudo 2>&1 | tail -3

for i in $(seq 1 15); do
    if getent hosts github.com &>/dev/null; then
        break
    fi
    echo "[raava] Waiting for DNS... ($i/15)"
    sleep 2
done

if [ ! -d /home/agent/hermes-agent ]; then
    echo "[raava] Installing Hermes agent..."
    cd /home/agent
    git clone https://github.com/NousResearch/hermes-agent.git
else
    echo "[raava] Hermes already installed; reconciling ref and dependencies"
fi

cd /home/agent/hermes-agent
git fetch --tags --force origin
git checkout --detach "$HERMES_REF"
ensure_python_venv
python3 -m venv venv
source venv/bin/activate
pip install -e ".[$INSTALL_EXTRAS]" -q
if [[ "$INSTALL_EXTRAS" == *"messaging"* ]] || [[ "$INSTALL_EXTRAS" == "all" ]]; then
    python -c "import discord" 2>/dev/null && echo "[raava] discord.py OK" || echo "[raava] WARNING: discord.py not installed"
fi
chown -R agent:agent /home/agent/hermes-agent

echo "[raava] Installing Envoy configs..."
install -d -m 0700 -o agent -g agent /home/agent/.hermes
install -d -m 0700 -o agent -g agent /home/agent/.hermes/sessions
install -d -m 0700 -o agent -g agent /home/agent/.hermes/memories
install -d -m 0700 -o agent -g agent /home/agent/.hermes/skills
install -d -m 0750 -o agent -g agent /home/agent/.hermes/logs
install -d -m 0750 -o agent -g agent /home/agent/workspace
install -d -m 0750 -o agent -g agent /home/agent/.hermes/envoy
install -d -m 0750 -o agent -g agent /home/agent/.hermes/envoy/memory/companies

cp /tmp/raava-provision/config.yaml /home/agent/.hermes/config.yaml
chown agent:agent /home/agent/.hermes/config.yaml
chmod 600 /home/agent/.hermes/config.yaml

cp /tmp/raava-provision/SOUL.md /home/agent/.hermes/SOUL.md
chown agent:agent /home/agent/.hermes/SOUL.md
chmod 640 /home/agent/.hermes/SOUL.md

cp /tmp/raava-provision/paperclip-operator.py /home/agent/.hermes/envoy/paperclip_operator.py
chown agent:agent /home/agent/.hermes/envoy/paperclip_operator.py
chmod 750 /home/agent/.hermes/envoy/paperclip_operator.py

touch /home/agent/.hermes/envoy/memory/patterns.md
touch /home/agent/.hermes/envoy/memory/reasoning.md
chown -R agent:agent /home/agent/.hermes/envoy

echo "[raava] Installing Envoy Paperclip skills..."
installed_skills=0
for skill_file in /tmp/raava-provision/paperclip-skill-*.md; do
    [ -f "$skill_file" ] || continue
    installed_skills=$((installed_skills + 1))
    skill_name="$(basename "$skill_file" .md)"
    skill_name="${skill_name#paperclip-skill-}"
    skill_dir="/home/agent/.hermes/skills/paperclip-${skill_name}"
    install -d -m 0750 -o agent -g agent "$skill_dir"
    cp "$skill_file" "$skill_dir/SKILL.md"
    chown agent:agent "$skill_dir/SKILL.md"
    chmod 640 "$skill_dir/SKILL.md"
done
if [ "$installed_skills" -eq 0 ]; then
    echo "[raava] ERROR: no Paperclip skills found in /tmp/raava-provision" >&2
    exit 1
fi

cp /tmp/raava-provision/hermes-gateway.service /etc/systemd/system/hermes-gateway.service
chmod 644 /etc/systemd/system/hermes-gateway.service

mkdir -p /etc/hermes
if [ "$SECRETS_MODE" = "env" ]; then
    cp /tmp/raava-provision/env "/etc/hermes/${AGENT_SLUG}.env"
    chown root:agent "/etc/hermes/${AGENT_SLUG}.env"
    chmod 640 "/etc/hermes/${AGENT_SLUG}.env"
elif [ "$SECRETS_MODE" = "none" ]; then
    echo "[raava] secrets_mode=none - no env file installed"
else
    echo "[raava] ERROR: unsupported SECRETS_MODE=${SECRETS_MODE}" >&2
    exit 1
fi

systemctl daemon-reload
systemctl enable hermes-gateway
systemctl restart hermes-gateway

echo "[raava] Verifying Envoy gateway..."
for i in $(seq 1 15); do
    if systemctl is-active --quiet hermes-gateway; then
        break
    fi
    sleep 2
done

if ! systemctl is-active --quiet hermes-gateway; then
    echo "[raava] WARNING: Envoy gateway failed to start"
    journalctl -u hermes-gateway -n 20 --no-pager
    exit 1
fi

sleep 5
if systemctl is-active --quiet hermes-gateway; then
    PID=$(pgrep -f "hermes_cli.main gateway" -o 2>/dev/null || echo "unknown")
    echo "[raava] Envoy verified stable (PID: $PID)"
else
    echo "[raava] WARNING: Envoy gateway started but crashed within 5s"
    journalctl -u hermes-gateway -n 20 --no-pager
    exit 1
fi

echo "[raava] Envoy provisioning complete"
