#!/bin/bash
set -e

AGENT_NAME="${1:?Usage: common-setup.sh <agent_name>}"

echo "[raava] Common setup for $AGENT_NAME"

# Bootstrap base tooling on bare images (plain docker ubuntu has no curl;
# LXD/GCE golden images ship with it — this is a no-op there).
if ! command -v curl &>/dev/null || ! command -v sudo &>/dev/null || ! command -v python3 &>/dev/null; then
    echo "[raava] Installing base tooling (curl, sudo, python3, ca-certificates)..."
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl sudo ca-certificates procps python3
fi

# Create agent user if not exists
if ! id agent &>/dev/null; then
    useradd -m -s /bin/bash agent
    echo "[raava] Created user: agent"
fi

# Create standard directories
mkdir -p /home/agent/.hermes
mkdir -p /home/agent/.openclaw
mkdir -p /etc/hermes
mkdir -p /etc/openclaw
chown -R agent:agent /home/agent

# Install 1Password CLI if not present
if ! command -v op &>/dev/null; then
    echo "[raava] Installing 1Password CLI..."
    ARCH="$(dpkg --print-architecture)"
    case "$ARCH" in
        amd64|arm64|arm|386) ;;
        *)
            echo "[raava] Unsupported architecture for 1Password CLI: $ARCH" >&2
            exit 1
            ;;
    esac
    OP_DEB_URL="https://downloads.1password.com/linux/debian/${ARCH}/stable/1password-cli-${ARCH}-latest.deb"
    OP_DEB_PATH="$(mktemp /tmp/1password-cli.XXXXXX.deb)"
    curl -fsSL "$OP_DEB_URL" -o "$OP_DEB_PATH"
    apt-get update -qq
    apt-get install -y -qq "$OP_DEB_PATH"
    rm -f "$OP_DEB_PATH"
    echo "[raava] 1Password CLI installed: $(op --version)"
else
    echo "[raava] 1Password CLI already installed: $(op --version)"
fi

echo "[raava] Common setup complete"
