#!/bin/bash
set -e

AGENT_SLUG="${1:?Usage: provision.sh <agent_slug>}"
HERMES_REF="${HERMES_REF:-c0aa06f300e5a38afcea6de79330f76b0d01c934}"

# systemd is not available inside plain Docker containers. The provisioner sets
# NO_SYSTEMD=1 for the docker provider; the runtime probe also covers any host
# where systemctl is simply absent (e.g. a freshly pulled base image).
has_systemd() {
    [ "${NO_SYSTEMD:-0}" != "1" ] \
        && command -v systemctl >/dev/null 2>&1 \
        && [ -d /run/systemd/system ]
}

ensure_python_venv() {
    python3 - <<'PY' >/dev/null 2>&1
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

echo "[raava] Provisioning Hermes Direct agent: $AGENT_SLUG"

if ! command -v git &>/dev/null || ! ensure_python_venv; then
    echo "[raava] Installing required packages..."
    retry_apt apt-get update -qq
    retry_apt apt-get install -y -qq git python3-venv 2>&1 | tail -3
fi

for i in $(seq 1 15); do
    # getent ships with glibc everywhere; `host` (dnsutils) is absent on
    # bare images like docker ubuntu:24.04.
    if getent hosts github.com &>/dev/null || host github.com &>/dev/null; then
        break
    fi
    echo "[raava] Waiting for DNS... ($i/15)"
    sleep 2
done

if [ ! -d /home/agent/hermes-agent ]; then
    echo "[raava] Installing Hermes agent..."
    cd /home/agent
    # HERMES_REF reaches git argv — reject anything that could smuggle a
    # flag (leading dash) or shell-ish characters. Allow branch/tag/SHA
    # shapes only: alnum start, then alnum . _ / -
    case "$HERMES_REF" in
        -*|*[!a-zA-Z0-9._/-]*|"")
            echo "[raava] ERROR: invalid HERMES_REF '$HERMES_REF'" >&2
            exit 1
            ;;
    esac
    # HERMES_REF may be a branch/tag OR a commit SHA. `git clone --branch`
    # only accepts branches/tags, so fall back to fetch+checkout for SHAs.
    if git clone --branch "$HERMES_REF" --depth 1 https://github.com/NousResearch/hermes-agent.git 2>/dev/null; then
        :
    else
        echo "[raava] '$HERMES_REF' is not a branch/tag — fetching as commit"
        git clone --depth 1 https://github.com/NousResearch/hermes-agent.git
        cd hermes-agent
        git fetch --depth 1 origin "$HERMES_REF"
        git checkout --quiet "$HERMES_REF"
        cd ..
    fi
    cd hermes-agent
    python3 -m venv venv
    source venv/bin/activate
    pip install -e . -q
    chown -R agent:agent /home/agent/hermes-agent
    echo "[raava] Hermes installed"
else
    echo "[raava] Hermes already installed"
fi

echo "[raava] Installing configs..."
mkdir -p /home/agent/.hermes
# The dir is created as root; the gateway runs as agent and must be able to
# create runtime subdirs (logs/, sessions/, …) — own the whole tree to agent.
chown -R agent:agent /home/agent/.hermes

cp /tmp/raava-provision/config.yaml /home/agent/.hermes/config.yaml
chown agent:agent /home/agent/.hermes/config.yaml
chmod 600 /home/agent/.hermes/config.yaml

cp /tmp/raava-provision/SOUL.md /home/agent/.hermes/SOUL.md
chown agent:agent /home/agent/.hermes/SOUL.md

mkdir -p /etc/hermes
cp /tmp/raava-provision/env /etc/hermes/${AGENT_SLUG}.env
chmod 600 /etc/hermes/${AGENT_SLUG}.env

# Bridge mode (credentials-vault §U5): the rendered OPENROUTER_BASE_URL carries a
# render-time gateway placeholder (172.31.0.1) because the Fleet API does not know
# the per-VM /30 index. Rewrite the host octet to this guest's actual default
# gateway (its /30 .1, where the host bridge LLM gateway listens). No-op when the
# placeholder is absent (compatibility lane).
if [ "${SECRETS_MODE:-env}" = "bridge" ]; then
    GW="$(ip route show default 2>/dev/null | awk '/default/ {print $3; exit}')"
    if [ -n "$GW" ]; then
        sed -i "s|http://172\.31\.0\.1:|http://${GW}:|g" /etc/hermes/${AGENT_SLUG}.env
        sed -i "s|http://172\.31\.0\.1:|http://${GW}:|g" /home/agent/.hermes/config.yaml
        echo "[raava] Bridge mode: OPENROUTER_BASE_URL points at gateway ${GW}"
    fi
fi

if has_systemd; then
    cp /tmp/raava-provision/hermes-gateway.service /etc/systemd/system/hermes-gateway.service
    chmod 644 /etc/systemd/system/hermes-gateway.service

    systemctl daemon-reload
    systemctl enable hermes-gateway
    systemctl start hermes-gateway
    echo "[raava] Hermes gateway started via systemd"
else
    echo "[raava] systemd unavailable — launching hermes gateway as a background process"
    mkdir -p /var/log/raava
    # Mirror the ExecStart from hermes-gateway.service without systemd. The env
    # file is sourced explicitly since there is no systemd EnvironmentFile.
    # Drop to the agent user (matches the systemd unit's User=agent) so
    # ~/.hermes state files are agent-owned — a root-owned state dir breaks
    # later agent-user invocations (e.g. the chat one-shot relay).
    setsid bash -c '
        set -a
        [ -f /etc/hermes/'"${AGENT_SLUG}"'.env ] && . /etc/hermes/'"${AGENT_SLUG}"'.env
        set +a
        exec sudo -u agent --preserve-env=OPENROUTER_API_KEY,OPENROUTER_BASE_URL,OPENAI_API_KEY,OPENAI_BASE_URL,ANTHROPIC_API_KEY \
            env HOME=/home/agent HERMES_HOME=/home/agent/.hermes \
            /home/agent/hermes-agent/venv/bin/python -m hermes_cli.main gateway run
    ' >/var/log/raava/hermes-gateway.log 2>&1 < /dev/null &
    echo "[raava] Hermes gateway started as direct process"
fi

sleep 3

if pgrep -f "hermes_cli.main gateway" > /dev/null; then
    PID=$(pgrep -f "hermes_cli.main gateway" -o)
    echo "[raava] Agent verified running, PID: $PID"
else
    echo "[raava] WARNING: Agent process not detected after start"
    if has_systemd; then
        journalctl -u hermes-gateway -n 15 --no-pager || true
    else
        tail -n 15 /var/log/raava/hermes-gateway.log 2>/dev/null || true
    fi
    exit 1
fi

echo "[raava] Provisioning complete"
