#!/bin/bash
set -e

AGENT_SLUG="${1:?Usage: provision.sh <agent_slug>}"

echo "[raava] Provisioning Smoke agent: $AGENT_SLUG"

# systemd is not available inside plain Docker containers. When the provisioner
# launches against the docker provider it sets NO_SYSTEMD=1 (and the runtime
# auto-detection below also covers the case where systemctl is simply absent).
has_systemd() {
    [ "${NO_SYSTEMD:-0}" != "1" ] \
        && command -v systemctl >/dev/null 2>&1 \
        && [ -d /run/systemd/system ]
}

for i in $(seq 1 15); do
    # getent ships with glibc everywhere; `host` (dnsutils) is absent on
    # bare images like docker ubuntu:24.04.
    if getent hosts github.com &>/dev/null || host github.com &>/dev/null; then
        break
    fi
    echo "[raava] Waiting for DNS... ($i/15)"
    sleep 2
done

# Ensure the agent user exists even on minimal base images (e.g. ubuntu:24.04
# pulled straight by the docker provider, where common-setup may not have run).
if ! id agent &>/dev/null; then
    useradd -m -s /bin/bash agent 2>/dev/null || true
fi

mkdir -p /home/agent/.smoke/workspace
cp /tmp/raava-provision/SOUL.md /home/agent/.smoke/workspace/SOUL.md
cat >/home/agent/.smoke/workspace/index.html <<EOF
<html><body><h1>Raava smoke test</h1><p>${AGENT_SLUG}</p></body></html>
EOF
chown -R agent:agent /home/agent/.smoke 2>/dev/null || true

if has_systemd; then
    cp /tmp/raava-provision/smoke-gateway.service /etc/systemd/system/smoke-gateway.service
    chmod 644 /etc/systemd/system/smoke-gateway.service

    systemctl daemon-reload
    systemctl enable smoke-gateway
    systemctl start smoke-gateway
    echo "[raava] Smoke gateway started via systemd"
else
    echo "[raava] systemd unavailable — launching smoke gateway as a background process"
    # Mirror the ExecStart from smoke-gateway.service without systemd.
    mkdir -p /var/log/raava
    setsid python3 -m http.server 18888 --bind 0.0.0.0 \
        --directory /home/agent/.smoke/workspace \
        >/var/log/raava/smoke-gateway.log 2>&1 < /dev/null &
    echo "[raava] Smoke gateway started as direct process"
fi

sleep 3

if pgrep -f "http.server 18888" > /dev/null; then
    PID=$(pgrep -o -f "http.server 18888")
    echo "[raava] Smoke process verified running, PID: $PID"
else
    echo "[raava] WARNING: Smoke process not detected after start"
    if has_systemd; then
        journalctl -u smoke-gateway -n 20 --no-pager || true
    else
        tail -n 20 /var/log/raava/smoke-gateway.log 2>/dev/null || true
    fi
    exit 1
fi

echo "[raava] Provisioning complete"
