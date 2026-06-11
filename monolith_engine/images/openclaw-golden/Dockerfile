# OpenClaw golden image for the docker provider.
#
# Bakes Node 22 + the openclaw package so a docker provision is
# config-render + gateway-start. templates/openclaw/provision.sh skips
# installs that are already present, so the same script runs on this image
# and on a bare base.
FROM ubuntu:24.04

# Resolved to an exact version by scripts/build-golden-images.sh; "latest"
# is only the fallback when building by hand.
ARG OPENCLAW_VERSION=latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        ca-certificates curl gnupg procps sudo \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y -qq nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g --quiet "openclaw@${OPENCLAW_VERSION}"

# uid/gid parity with the provisioner's common-setup (see hermes-golden).
RUN groupadd -g 1001 agent && useradd -m -s /bin/bash -u 1001 -g 1001 agent

CMD ["sleep", "infinity"]
