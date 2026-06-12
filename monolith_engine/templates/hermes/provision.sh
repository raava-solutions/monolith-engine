#!/bin/bash
set -e

AGENT_SLUG="${1:?Usage: provision.sh <agent_slug>}"

# systemd is not available inside plain Docker containers. The provisioner
# sets NO_SYSTEMD=1 for the docker provider; the runtime probe also covers
# hosts where systemctl is simply absent.
has_systemd() {
    [ "${NO_SYSTEMD:-0}" != "1" ] \
        && command -v systemctl >/dev/null 2>&1 \
        && [ -d /run/systemd/system ]
}

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

# Ensure required packages are installed
echo "[raava] Ensuring required packages are installed..."
retry_apt apt-get update -qq
retry_apt apt-get install -y -qq git python3-venv sudo 2>&1 | tail -3

# Configurable via environment variables
SECRETS_MODE="${SECRETS_MODE:-env}"
HERMES_REF="${HERMES_REF:-latest}"
INSTALL_EXTRAS="${INSTALL_EXTRAS:-messaging}"

echo "[raava] Provisioning Hermes agent: $AGENT_SLUG"
echo "[raava] secrets_mode=$SECRETS_MODE hermes_ref=$HERMES_REF install_extras=$INSTALL_EXTRAS"

# Wait for DNS to be ready (network may have IP but DNS not resolving yet)
for i in $(seq 1 15); do
    if getent hosts github.com &>/dev/null; then
        break
    fi
    echo "[raava] Waiting for DNS... ($i/15)"
    sleep 2
done

# Install Hermes if not present
if [ ! -d /home/agent/hermes-agent ]; then
    echo "[raava] Installing Hermes agent..."
    cd /home/agent

    if [ "$HERMES_REF" = "latest" ]; then
        git clone --depth 1 https://github.com/NousResearch/hermes-agent.git
    else
        git clone https://github.com/NousResearch/hermes-agent.git
        cd hermes-agent
        git checkout "$HERMES_REF"
        cd /home/agent
    fi

    cd hermes-agent
    ensure_python_venv
    python3 -m venv venv
    source venv/bin/activate
    pip install -e ".[$INSTALL_EXTRAS]" -q

    # Verify critical packages based on install_extras
    if [[ "$INSTALL_EXTRAS" == *"messaging"* ]] || [[ "$INSTALL_EXTRAS" == "all" ]]; then
        echo "[raava] Verifying messaging dependencies..."
        python -c "import discord" 2>/dev/null && echo "[raava] discord.py OK" || echo "[raava] WARNING: discord.py not installed"
        python -c "import telegram" 2>/dev/null && echo "[raava] python-telegram-bot OK" || echo "[raava] WARNING: python-telegram-bot not installed"
    fi

    chown -R agent:agent /home/agent/hermes-agent
    echo "[raava] Hermes installed"
else
    echo "[raava] Hermes already installed"
fi

# Move configs into place
echo "[raava] Installing configs..."
install -d -m 0700 -o agent -g agent /home/agent/.hermes
install -d -m 0700 -o agent -g agent /home/agent/.hermes/sessions
install -d -m 0700 -o agent -g agent /home/agent/.hermes/memories
install -d -m 0700 -o agent -g agent /home/agent/.hermes/skills
install -d -m 0750 -o agent -g agent /home/agent/.hermes/logs
install -d -m 0750 -o agent -g agent /home/agent/workspace

cp /tmp/raava-provision/config.yaml /home/agent/.hermes/config.yaml
chown agent:agent /home/agent/.hermes/config.yaml
chmod 600 /home/agent/.hermes/config.yaml

cp /tmp/raava-provision/SOUL.md /home/agent/.hermes/SOUL.md
chown agent:agent /home/agent/.hermes/SOUL.md

cp /tmp/raava-provision/hermes-gateway.service /etc/systemd/system/hermes-gateway.service
chmod 644 /etc/systemd/system/hermes-gateway.service

if [ -f /tmp/raava-provision/paperclip-agent-gateway.py ] && [ -f /tmp/raava-provision/paperclip-agent-gateway.service ]; then
    install -d -m 0755 /opt/paperclip-agent-gateway
    install -d -m 0750 -o agent -g agent /var/log/paperclip-agent-gateway
    cp /tmp/raava-provision/paperclip-agent-gateway.py /opt/paperclip-agent-gateway/server.py
    chmod 755 /opt/paperclip-agent-gateway/server.py
    cp /tmp/raava-provision/paperclip-agent-gateway.service /etc/systemd/system/paperclip-agent-gateway.service
    chmod 644 /etc/systemd/system/paperclip-agent-gateway.service
fi

# Ensure the hermes config dir exists before any /etc/hermes writes below.
mkdir -p /etc/hermes

HUD_COLLECTOR_ENABLED=false
if [ -f /tmp/raava-provision/env ] && grep -q '^MONOLITH_HUD_ENABLED=true' /tmp/raava-provision/env; then
    HUD_COLLECTOR_ENABLED=true
fi

if [ "$HUD_COLLECTOR_ENABLED" = "true" ] \
    && [ -f /tmp/raava-provision/monolith-hud-collector.py ] \
    && [ -f /tmp/raava-provision/monolith-hud-collector.service ] \
    && [ -f /tmp/raava-provision/monolith-hud-collector.timer ]; then
    cp /tmp/raava-provision/monolith-hud-collector.py /etc/hermes/monolith-hud-collector.py
    chown root:agent /etc/hermes/monolith-hud-collector.py
    chmod 750 /etc/hermes/monolith-hud-collector.py
    cp /tmp/raava-provision/monolith-hud-collector.service /etc/systemd/system/monolith-hud-collector.service
    cp /tmp/raava-provision/monolith-hud-collector.timer /etc/systemd/system/monolith-hud-collector.timer
    chmod 644 /etc/systemd/system/monolith-hud-collector.service /etc/systemd/system/monolith-hud-collector.timer
else
    systemctl disable --now monolith-hud-collector.timer >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/monolith-hud-collector.service
    rm -f /etc/systemd/system/monolith-hud-collector.timer
    rm -f /etc/hermes/monolith-hud-collector.py
fi

if grep -qx "enabled" /tmp/raava-provision/aurum-steering-enabled.flag \
    && [ -f /tmp/raava-provision/aurum-steering-plugin.yaml ] \
    && [ -f /tmp/raava-provision/aurum-steering-__init__.py ]; then
    install -d -m 0755 -o agent -g agent /home/agent/.hermes/plugins/aurum-steering
    cp /tmp/raava-provision/aurum-steering-plugin.yaml /home/agent/.hermes/plugins/aurum-steering/plugin.yaml
    cp /tmp/raava-provision/aurum-steering-__init__.py /home/agent/.hermes/plugins/aurum-steering/__init__.py
    chown -R agent:agent /home/agent/.hermes/plugins/aurum-steering
    chmod 644 /home/agent/.hermes/plugins/aurum-steering/plugin.yaml
    chmod 644 /home/agent/.hermes/plugins/aurum-steering/__init__.py
    install -d -m 0755 -o agent -g agent /home/agent/.hermes/aurum
    if [ -f /tmp/raava-provision/aurum-skills.md ]; then
        cp /tmp/raava-provision/aurum-skills.md /home/agent/.hermes/aurum/skills.md
        chown agent:agent /home/agent/.hermes/aurum/skills.md
        chmod 644 /home/agent/.hermes/aurum/skills.md
    fi
    if [ -f /tmp/raava-provision/aurum-trading_system.md ]; then
        cp /tmp/raava-provision/aurum-trading_system.md /home/agent/.hermes/aurum/trading_system.md
        chown agent:agent /home/agent/.hermes/aurum/trading_system.md
        chmod 644 /home/agent/.hermes/aurum/trading_system.md
    fi
elif [ -d /home/agent/.hermes/plugins/aurum-steering ]; then
    rm -rf /home/agent/.hermes/plugins/aurum-steering
fi

# Env file handling depends on secrets_mode
mkdir -p /etc/hermes

if [ "$SECRETS_MODE" = "env" ]; then
    cp /tmp/raava-provision/env "/etc/hermes/${AGENT_SLUG}.env"
    chown root:agent "/etc/hermes/${AGENT_SLUG}.env"
    chmod 640 "/etc/hermes/${AGENT_SLUG}.env"
else
    echo "[raava] secrets_mode=none — no env file installed"
fi

# Enable and start
if has_systemd; then
    systemctl daemon-reload
    systemctl enable hermes-gateway
    systemctl start hermes-gateway
    if [ -f /etc/systemd/system/paperclip-agent-gateway.service ] && grep -q '^PAPERCLIP_AGENT_ID=' "/etc/hermes/${AGENT_SLUG}.env" 2>/dev/null; then
        systemctl enable paperclip-agent-gateway
        systemctl restart paperclip-agent-gateway
    fi
    if [ -f /etc/systemd/system/monolith-hud-collector.timer ]; then
        if grep -q '^MONOLITH_HUD_INGEST_URL=' "/etc/hermes/${AGENT_SLUG}.env" 2>/dev/null \
            && grep -Eq '^(MONOLITH_HUD_INGEST_API_KEY|MONOLITH_TRACE_INGEST_API_KEY)=' "/etc/hermes/${AGENT_SLUG}.env" 2>/dev/null; then
            systemctl enable --now monolith-hud-collector.timer
        else
            echo "[raava] HUD collector installed but timer disabled; missing ingest URL or API key"
        fi
    fi
else
    echo "[raava] systemd unavailable — launching hermes gateway as a background process"
    mkdir -p /var/log/raava
    # Mirror the ExecStart from hermes-gateway.service without systemd
    # (same launch as templates/hermes-direct). Paperclip/HUD units are
    # systemd-only and stay off in this lane.
    setsid bash -c '
        set -a
        [ -f /etc/hermes/'"${AGENT_SLUG}"'.env ] && . /etc/hermes/'"${AGENT_SLUG}"'.env
        set +a
        exec sudo -u agent --preserve-env=OPENROUTER_API_KEY,OPENROUTER_BASE_URL,OPENAI_API_KEY,OPENAI_BASE_URL,ANTHROPIC_API_KEY \
            env HOME=/home/agent HERMES_HOME=/home/agent/.hermes \
            /home/agent/hermes-agent/venv/bin/python -m hermes_cli.main gateway run
    ' >/var/log/raava/hermes-gateway.log 2>&1 < /dev/null &
fi

echo "[raava] Verifying gateway..."
if has_systemd; then
    for i in $(seq 1 15); do
        if systemctl is-active --quiet hermes-gateway; then
            break
        fi
        sleep 2
    done

    if ! systemctl is-active --quiet hermes-gateway; then
        echo "[raava] WARNING: Gateway failed to start"
        journalctl -u hermes-gateway -n 20 --no-pager
        exit 1
    fi

    # Confirm it stays running for 5 seconds (not a crash loop)
    sleep 5
    if systemctl is-active --quiet hermes-gateway; then
        RESTARTS=$(systemctl show hermes-gateway --property=NRestarts --value 2>/dev/null || echo "0")
        PID=$(pgrep -f "hermes_cli.main gateway" -o 2>/dev/null || echo "unknown")
        echo "[raava] Agent verified stable (PID: $PID, restarts: $RESTARTS)"
    else
        echo "[raava] WARNING: Gateway started but crashed within 5s"
        journalctl -u hermes-gateway -n 20 --no-pager
        exit 1
    fi
else
    sleep 5
    if pgrep -f "hermes_cli.main gateway" > /dev/null; then
        PID=$(pgrep -f "hermes_cli.main gateway" -o)
        echo "[raava] Agent verified running, PID: $PID"
    else
        echo "[raava] WARNING: Agent process not detected after start"
        tail -n 20 /var/log/raava/hermes-gateway.log 2>/dev/null || true
        exit 1
    fi
fi

if has_systemd && [ -f /etc/systemd/system/paperclip-agent-gateway.service ] && grep -q '^PAPERCLIP_AGENT_ID=' /etc/hermes/${AGENT_SLUG}.env 2>/dev/null; then
    if systemctl is-active --quiet paperclip-agent-gateway; then
        echo "[raava] Paperclip gateway active"
    else
        echo "[raava] WARNING: Paperclip gateway failed to start"
        journalctl -u paperclip-agent-gateway -n 20 --no-pager
        exit 1
    fi
fi

# gbrain shim install (raava-tenant agents only — provisioner sets
# GBRAIN_CLIENT_ID only when tenant_id=raava AND gbrain_enabled, so a
# non-empty value here is the install signal. The bootstrap script also
# enforces a runtime VM-metadata tenant guard as defense-in-depth).
# Failure is logged WARN but does not abort provisioning.
if has_systemd && grep -qE '^GBRAIN_CLIENT_ID=[^[:space:]]+' /tmp/raava-provision/gbrain.env 2>/dev/null && [ -f /tmp/raava-provision/bootstrap-gbrain.sh ]; then
    echo "[raava] Wiring gbrain shim..."
    install -m 0755 /tmp/raava-provision/gbrain-mcp /usr/local/bin/gbrain
    mkdir -p /etc/raava /etc/systemd/system/hermes-gateway.service.d
    install -m 0640 -o root -g agent /tmp/raava-provision/gbrain.env /etc/raava/gbrain.env
    install -m 0644 /tmp/raava-provision/gbrain.conf /etc/systemd/system/hermes-gateway.service.d/gbrain.conf
    # Drop the gbrain-first reflex skill into Hermes' skills dir so the agent
    # knows to query the company brain before web/session search.
    if [ -f /tmp/raava-provision/gbrain-first.md ]; then
        install -d -m 0700 -o agent -g agent /home/agent/.hermes/skills
        install -m 0644 -o agent -g agent /tmp/raava-provision/gbrain-first.md /home/agent/.hermes/skills/gbrain-first.md
    fi
    systemctl daemon-reload
    if bash /tmp/raava-provision/bootstrap-gbrain.sh "$AGENT_SLUG"; then
        echo "[raava] gbrain bootstrap OK; restarting hermes-gateway to pick up env"
        systemctl restart hermes-gateway
        sleep 3
        if ! systemctl is-active --quiet hermes-gateway; then
            echo "[raava] WARNING: gateway failed post-gbrain restart"
            journalctl -u hermes-gateway -n 20 --no-pager
        fi
    else
        echo "[raava] WARNING: gbrain bootstrap failed; agent will run without gbrain"
    fi
fi

echo "[raava] Provisioning complete"
