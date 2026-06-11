# Hermes golden image for the docker provider.
#
# Bakes everything templates/hermes-direct/provision.sh would otherwise
# install at provision time (apt deps, hermes-agent checkout, venv) so a
# docker provision is config-render + gateway-start. provision.sh stays
# idempotent (skip-if-present), so the same script runs on this image and on
# a bare ubuntu base.
FROM ubuntu:24.04

# Keep in sync with HERMES_REF in templates/hermes-direct/provision.sh.
ARG HERMES_REF=c0aa06f300e5a38afcea6de79330f76b0d01c934

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        ca-certificates curl git procps python3-venv sudo \
    && rm -rf /var/lib/apt/lists/*

# uid/gid 1001: ubuntu:24.04 ships the `ubuntu` user at 1000; the
# provisioner's common-setup step creates agent at the next free uid (1001)
# on bare images, so pin the same here for path/ownership parity.
RUN groupadd -g 1001 agent && useradd -m -s /bin/bash -u 1001 -g 1001 agent

USER agent
WORKDIR /home/agent
RUN set -eux; \
    if git clone --branch "$HERMES_REF" --depth 1 https://github.com/NousResearch/hermes-agent.git 2>/dev/null; then \
        :; \
    else \
        git clone --depth 1 https://github.com/NousResearch/hermes-agent.git; \
        cd hermes-agent; \
        git fetch --depth 1 origin "$HERMES_REF"; \
        git checkout --quiet "$HERMES_REF"; \
        cd ..; \
    fi; \
    cd hermes-agent; \
    python3 -m venv venv; \
    ./venv/bin/pip install --no-cache-dir -q -e ".[messaging]" croniter

USER root
# Fleet containers are long-lived compute; the provider pins this anyway.
CMD ["sleep", "infinity"]
