#!/usr/bin/env python3
"""Fake ``docker exec`` for Agent 11 tests.

Mimics the subset of ``docker`` the container backend uses:

    docker exec -i [-e K=V]... [-w DIR] NAME sh -c SCRIPT _ CMD...
    docker exec NAME kill -<sig> <pid>

The backend wraps every command as ``sh -c 'echo "__CAIRN_PID__:$$" >&2; exec "$@"'
_ CMD...`` so the in-container "PID" (a real host PID here) appears on stderr as a
marker line that ``LocalProcess`` strips. This lets tests exercise the full
ContainerProcess exec path without a Docker daemon.

The container NAME is ignored; only the env/-w flags and the wrapped command matter.
"""
from __future__ import annotations

import os
import subprocess
import sys


def _main() -> int:
    args = sys.argv[1:]
    # docker exec ... → skip the subcommand token first
    if args and args[0] == "exec":
        args = args[1:]
    envs: dict[str, str] = {}
    cmd: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-e":
            if i + 1 < len(args):
                k, _, v = args[i + 1].partition("=")
                envs[k] = v
                i += 2
                continue
        elif a in ("-i", "-t", "--interactive"):
            i += 1
            continue
        elif a == "-w":
            i += 2  # working dir: irrelevant for a fake
            continue
        elif a.startswith("-"):
            i += 1
            continue
        else:
            cmd = args[i + 1:]  # skip container NAME
            break

    os.environ.update(envs)

    if not cmd:
        return 0

    # docker exec NAME kill -<sig> <pid>  → return 0 (killed "inside container")
    if cmd[0] == "kill":
        return 0

    # docker exec NAME sh -c SCRIPT _ CMD...  → run the sh wrapper
    if cmd[0] == "sh" and len(cmd) >= 3 and cmd[1] == "-c":
        script = cmd[2]
        rest = cmd[3:]  # ["_", *CMD...]
        p = subprocess.run(["sh", "-c", script, *rest], env=os.environ)
        return p.returncode

    # fallback: run the command directly
    p = subprocess.run(cmd, env=os.environ)
    return p.returncode


if __name__ == "__main__":
    sys.exit(_main())
