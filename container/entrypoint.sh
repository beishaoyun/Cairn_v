#!/usr/bin/env bash
# Cairn worker container entrypoint.
#
# Best-effort, non-root setup. workspace/evidence are the only writable paths under a
# read-only root; their host-side bind sources are prepared by the Dispatcher for uid
# 1000 (the `worker` user), so the chown below is a no-op fallback when that holds and
# a harmless best-effort when the bind source is freshly created.
set -euo pipefail

mkdir -p /home/worker/workspace /home/worker/evidence
chown -R worker:worker /home/worker/workspace /home/worker/evidence 2>/dev/null || true

exec "$@"
