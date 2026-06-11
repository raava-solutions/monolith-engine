#!/bin/bash
set -e

AGENT_SLUG="${1:?Usage: provision.sh <agent_slug>}"

# systemd is not available inside plain Docker containers. The provisioner
# sets NO_SYSTEMD=1 for the docker provider.
has_systemd() {
    [ "${NO_SYSTEMD:-0}" != "1" ] \
        && command -v systemctl >/dev/null 2>&1 \
        && [ -d /run/systemd/system ]
}

echo "[raava] Provisioning OpenClaw agent: $AGENT_SLUG"

# Wait for DNS
for i in $(seq 1 15); do
    if getent hosts github.com &>/dev/null || host github.com &>/dev/null; then
        break
    fi
    echo "[raava] Waiting for DNS... ($i/15)"
    sleep 2
done

# Upgrade Node.js if needed (OpenClaw requires 22.12+)
NODE_VER=$(node --version 2>/dev/null | sed 's/v//' | cut -d. -f1)
if [ -z "$NODE_VER" ] || [ "$NODE_VER" -lt 22 ]; then
    echo "[raava] Upgrading Node.js to v22..."
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - 2>&1 | tail -3
    apt-get install -y nodejs -qq
    echo "[raava] Node.js upgraded: $(node --version)"
fi

# Install OpenClaw if not present
if ! command -v openclaw &>/dev/null; then
    echo "[raava] Installing OpenClaw..."
    npm install -g openclaw -q 2>&1 | tail -3
    echo "[raava] OpenClaw installed: $(openclaw --version 2>/dev/null || echo 'installed')"
else
    echo "[raava] OpenClaw already installed: $(openclaw --version 2>/dev/null || echo 'present')"
fi

# Initialize workspace
mkdir -p /home/agent/.openclaw/{workspace,agents/main/{agent,sessions,workspace},logs}

# Move configs into place
echo "[raava] Installing configs..."

cp /tmp/raava-provision/openclaw.json /home/agent/.openclaw/openclaw.json
chown agent:agent /home/agent/.openclaw/openclaw.json
chmod 600 /home/agent/.openclaw/openclaw.json
cp /tmp/raava-provision/SOUL.md /home/agent/.openclaw/workspace/SOUL.md
chown agent:agent /home/agent/.openclaw/workspace/SOUL.md

if has_systemd; then
    cp /tmp/raava-provision/openclaw-gateway.service /etc/systemd/system/openclaw-gateway.service
    chmod 644 /etc/systemd/system/openclaw-gateway.service
fi

mkdir -p /etc/openclaw
cp /tmp/raava-provision/env.tpl /etc/openclaw/${AGENT_SLUG}.env.tpl
chown agent:agent /etc/openclaw/${AGENT_SLUG}.env.tpl
chmod 600 /etc/openclaw/${AGENT_SLUG}.env.tpl
if [ -f /tmp/raava-provision/env ]; then
    # Direct env lane (no 1Password wrapping) — used by docker/local mode.
    cp /tmp/raava-provision/env /etc/openclaw/${AGENT_SLUG}.env
    chown root:agent /etc/openclaw/${AGENT_SLUG}.env
    chmod 640 /etc/openclaw/${AGENT_SLUG}.env
fi

chown -R agent:agent /home/agent/.openclaw

# Enable and start
if has_systemd; then
    systemctl daemon-reload
    systemctl enable openclaw-gateway
    systemctl start openclaw-gateway
    echo "[raava] OpenClaw gateway started via systemd"
else
    echo "[raava] systemd unavailable — launching openclaw gateway as a background process"
    mkdir -p /var/log/raava
    GATEWAY_PORT="${GATEWAY_PORT:-18789}"
    # In containers the gateway defaults to bind=auto (0.0.0.0) and refuses
    # to start without auth — generate a per-agent gateway token once.
    touch "/etc/openclaw/${AGENT_SLUG}.env"
    if ! grep -q '^OPENCLAW_GATEWAY_TOKEN=' "/etc/openclaw/${AGENT_SLUG}.env"; then
        echo "OPENCLAW_GATEWAY_TOKEN=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> "/etc/openclaw/${AGENT_SLUG}.env"
    fi
    chown root:agent "/etc/openclaw/${AGENT_SLUG}.env"
    chmod 640 "/etc/openclaw/${AGENT_SLUG}.env"
    # Mirror the service ExecStart without systemd and without 1Password —
    # the direct env file is sourced explicitly.
    setsid bash -c '
        set -a
        [ -f /etc/openclaw/'"${AGENT_SLUG}"'.env ] && . /etc/openclaw/'"${AGENT_SLUG}"'.env
        set +a
        exec sudo -u agent --preserve-env=OPENROUTER_API_KEY,OPENROUTER_BASE_URL,OPENAI_API_KEY,OPENAI_BASE_URL,ANTHROPIC_API_KEY,OPENCLAW_GATEWAY_TOKEN \
            env HOME=/home/agent OPENCLAW_CONFIG_DIR=/home/agent/.openclaw OPENCLAW_WORKSPACE_DIR=/home/agent/.openclaw/workspace \
            openclaw gateway --port '"${GATEWAY_PORT:-18789}"'
    ' >/var/log/raava/openclaw-gateway.log 2>&1 < /dev/null &
fi

echo "[raava] OpenClaw gateway started"
sleep 5

if pgrep -f "openclaw" > /dev/null; then
    PID=$(pgrep -f "openclaw" -o)
    echo "[raava] Agent verified running, PID: $PID"
else
    echo "[raava] WARNING: Agent process not detected after start"
    if has_systemd; then
        journalctl -u openclaw-gateway -n 15 --no-pager
    else
        tail -n 15 /var/log/raava/openclaw-gateway.log 2>/dev/null || true
    fi
    exit 1
fi

echo "[raava] Provisioning complete"
