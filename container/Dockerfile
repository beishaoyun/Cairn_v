# syntax=docker/dockerfile:1
# Cairn v2 Worker sandbox image — minimal, non-root, no sudo, no docker.
# Spec: docs/worker-sandbox-hardening.md §2-§3, §5 (baseline checklist).
#
# Design intent:
#   * The image provides only the whitelisted toolset + Agent CLIs. It provides NO
#     privilege-escalation surface: non-root `worker` user, no sudo, no su, no setuid
#     helpers, no docker CLI, root filesystem made read-only at runtime (containers.py).
#   * Credentials are NEVER baked into image layers (C5 / worker-sandbox §4.2 / §8-9):
#     LLM keys arrive as runtime environment variables; the Cairn API token is never
#     injected into the agent container.
#   * Agent CLIs are Node packages; `nodejs` + `npm` are mandatory.
FROM debian:bookworm-slim

# ── Whitelisted toolset (install then purge caches) ────────────────────────
# Intentionally no: sudo / su / docker / docker-compose / perl-setuid tools.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates curl wget unzip zip jq \
        iputils-ping dnsutils netcat-openbsd \
        nmap sqlite3 python3 python3-pip \
        ripgrep fd-find git openssh-client \
        nodejs npm \
        python3-requests \
        && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── Non-root runtime user (no sudo group, no /etc/sudoers entry) ──────────
RUN useradd -m -s /bin/bash worker

# ── Agent CLIs (Node packages; non-root executable; credentials via env) ───
# Versions pinned per worker-sandbox-hardening §3. Bump deliberately.
RUN npm install -g \
        @anthropic-ai/claude-code@2.1.98 \
        @openai/codex@0.118.0 \
        @mariozechner/pi-coding-agent@0.73.0 \
    && npm cache clean --force

# ── Packaged resources: generic AGENTS.md initial context (prompts §10) ───
# Build context is the repo root (`docker build -f container/Dockerfile .`).
COPY container/AGENTS.md /home/worker/AGENTS.md
COPY container/entrypoint.sh /usr/local/bin/cairn-entrypoint.sh

# ── Workspace + evidence dirs; fix ownership ───────────────────────────────
RUN chmod +x /usr/local/bin/cairn-entrypoint.sh && \
    mkdir -p /home/worker/workspace /home/worker/evidence && \
    chown -R worker:worker /home/worker

WORKDIR /home/worker/workspace

# ── Runtime defaults ───────────────────────────────────────────────────────
# $HOME must point at the writable workspace volume (B6): claude/codex/pi write
# config + session state under ~/.claude, ~/.codex etc. Under a read-only root
# this is the only writable place those CLIs can use.
ENV HOME=/home/worker/workspace
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

ENTRYPOINT ["/usr/local/bin/cairn-entrypoint.sh"]
# Non-root. containers.py always launches with --user worker:worker too.
USER worker
