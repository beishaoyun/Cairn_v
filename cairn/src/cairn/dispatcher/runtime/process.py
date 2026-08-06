"""ExecProcess implementations (Agent 11).

Implements the ``ExecProcess`` protocol from ``runtime/backend.py``:

* ``LocalProcess`` — a host subprocess running in its own session/process group
  (``start_new_session=True``). ``kill(None)`` is the graceful path
  (SIGTERM → grace → SIGKILL); a concrete signal (e.g. ``signal.SIGKILL``) is sent
  immediately — the C1 kill-switch path (worker-sandbox-hardening §4.3).
* ``ContainerProcess`` — the ExecProcess view over a host-side ``docker exec`` client
  subprocess. Killing the client makes the Docker daemon terminate the in-container
  exec process; when the in-container PID is known (stderr marker) an explicit
  ``kill`` is also sent as a fallback ("managed container exec: kill inside the
  container, with a fallback", backend.py).

Both drain stdout/stderr on background threads (line-buffered) so a large or slow
output stream cannot deadlock ``communicate``; each drained line can be forwarded to
``on_line`` for the progress package (24) to write task_events chunk files.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
from pathlib import Path
from typing import Callable, Pattern, Sequence

from .backend import ExecProcess

#: stderr marker written by the in-container sh wrapper so the "container" PID is known.
CONTAINER_PID_MARKER = "__CAIRN_PID__"

#: (line, "stdout" | "stderr")
LineCallback = Callable[[str, str], None]


class ProcessError(Exception):
    """Execution backend / process error."""


def resolve_workspace_path(
    workspace_root: str | os.PathLike,
    project_id: str,
    rel_path: str,
) -> Path:
    """Resolve ``rel_path`` inside the project workspace, blocking traversal.

    graph §4-15 guard: any ``.``/``..`` segment is rejected and the resolved path must
    stay under ``<workspace_root>/<project_id>``. ``rel_path`` may be relative
    (recommended) or an absolute container path under ``/home/worker/workspace`` (the
    prefix is stripped first). Absolute paths elsewhere are treated as relative to the
    workspace — the file can never escape it.
    """
    base = (Path(workspace_root) / str(project_id)).resolve()
    p = rel_path.replace("\\", "/")

    container_prefix = "/home/worker/workspace"
    if p.startswith(container_prefix + "/"):
        p = p[len(container_prefix):]

    parts = [seg for seg in p.split("/") if seg]
    for seg in parts:
        if seg in (".", ".."):
            raise ValueError(f"路径穿越被拒绝（graph §4-15）: {rel_path!r}")
    if not parts:
        raise ValueError(f"rel_path 不能为空或指向工作区根（graph §4-15）: {rel_path!r}")

    target = base.joinpath(*parts).resolve()
    prefix = str(base) + os.sep
    if not str(target).startswith(prefix):
        raise ValueError(f"路径穿越被拒绝（graph §4-15）: {rel_path!r}")
    return target


def _drain(
    stream: object,
    buflist: list[str],
    on_line: LineCallback | None,
    stream_name: str,
    marker_re: Pattern[str] | None,
    marker_cb: Callable[[str], None] | None,
) -> None:
    try:
        for raw in iter(stream.readline, ""):  # type: ignore[attr-defined]
            if marker_re is not None:
                m = marker_re.match(raw)
                if m:
                    if marker_cb is not None:
                        try:
                            marker_cb(m.group(1))
                        except Exception:
                            pass
                    continue
            buflist.append(raw)
            if on_line is not None:
                try:
                    on_line(raw, stream_name)
                except Exception:
                    pass
    finally:
        try:
            stream.close()  # type: ignore[attr-defined]
        except Exception:
            pass


class LocalProcess:
    """ExecProcess backed by a host subprocess in its own session/process group."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | os.PathLike | None = None,
        timeout: float | None = None,
        grace_seconds: float = 10.0,
        stderr_marker: str | Pattern[str] | None = None,
        stderr_marker_cb: Callable[[str], None] | None = None,
        on_line: LineCallback | None = None,
    ) -> None:
        self._argv = [str(a) for a in argv]
        self._timeout = timeout
        self._grace = grace_seconds
        self._on_line = on_line
        self._timed_out = False
        self._lock = threading.Lock()
        self._out_buf: list[str] = []
        self._err_buf: list[str] = []

        self._proc = subprocess.Popen(
            self._argv,
            env=dict(env) if env is not None else None,
            cwd=str(cwd) if cwd is not None else None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,  # isolated process group for group-wide kills
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )
        try:
            self._pgid = os.getpgid(self._proc.pid)
        except ProcessLookupError:
            self._pgid = self._proc.pid
        self._drain_threads = self._start_drains(stderr_marker, stderr_marker_cb)

    # ------------------------------------------------------------------ protocol

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc.poll() is None else None

    @property
    def timed_out(self) -> bool:
        return self._timed_out

    @property
    def returncode(self) -> int | None:
        return self._proc.poll()

    def poll(self) -> int | None:
        return self._proc.poll()

    def communicate(
        self,
        input: str | None = None,
        timeout: float | None = None,
    ) -> tuple[str, str]:
        if input is not None:
            self._write_stdin(input)
        eff = self._timeout if timeout is None else timeout
        try:
            self._proc.wait(timeout=eff)
        except subprocess.TimeoutExpired:
            self._timed_out = True
            self.kill(None)  # graceful escalate: SIGTERM → grace → SIGKILL
            try:
                self._proc.wait(timeout=self._grace)
            except subprocess.TimeoutExpired:
                self._kill_group(signal.SIGKILL)
                try:
                    self._proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            self._close_stdin()
            self._join_drains()
        return "".join(self._out_buf), "".join(self._err_buf)

    def kill(self, sig: int | None = None) -> None:
        """Terminate the process group.

        ``sig is None`` → graceful (SIGTERM → grace → SIGKILL).
        A concrete signal (e.g. ``signal.SIGKILL``) is sent immediately — C1.
        """
        with self._lock:
            if sig is None:
                self._kill_group(signal.SIGTERM)
                try:
                    self._proc.wait(timeout=self._grace)
                except subprocess.TimeoutExpired:
                    self._kill_group(signal.SIGKILL)
                    try:
                        self._proc.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        pass
            else:
                self._kill_group(sig)

    # ------------------------------------------------------------------ internals

    def _start_drains(
        self,
        stderr_marker: str | Pattern[str] | None,
        stderr_marker_cb: Callable[[str], None] | None,
    ) -> list[threading.Thread]:
        marker = re.compile(stderr_marker) if isinstance(stderr_marker, str) else stderr_marker
        threads: list[threading.Thread] = []
        for stream, buf, name, m, cb in (
            (self._proc.stdout, self._out_buf, "stdout", None, None),
            (self._proc.stderr, self._err_buf, "stderr", marker, stderr_marker_cb),
        ):
            t = threading.Thread(
                target=_drain,
                args=(stream, buf, self._on_line, name, m, cb),
                daemon=True,
                name=f"cairn-drain-{name}",
            )
            t.start()
            threads.append(t)
        return threads

    def _write_stdin(self, data: str) -> None:
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass
        finally:
            self._close_stdin()

    def _close_stdin(self) -> None:
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        except Exception:
            pass

    def _join_drains(self) -> None:
        for t in self._drain_threads:
            t.join(timeout=max(0.1, self._grace))

    def _kill_group(self, sig: int) -> None:
        try:
            os.killpg(self._pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass


class ContainerProcess:
    """ExecProcess view over a managed-container ``docker exec`` client process.

    The wrapped ``LocalProcess`` is the host-side ``docker exec`` subprocess. Killing
    the client makes the Docker daemon terminate the in-container exec process; when
    the in-container PID is known (stderr marker) an explicit ``kill`` is also issued
    inside the container as a fallback.
    """

    def __init__(
        self,
        local: LocalProcess,
        *,
        container_name: str,
        container_pid: int | None = None,
        container_pid_fn: Callable[[], int | None] | None = None,
        docker_exec: Callable[[str, Sequence[str]], int] | None = None,
    ) -> None:
        self._local = local
        self._container_name = container_name
        self._container_pid = container_pid
        self._container_pid_fn = container_pid_fn
        self._docker_exec = docker_exec

    # ------------------------------------------------------------------ protocol

    @property
    def pid(self) -> int | None:
        return self._local.pid

    @property
    def timed_out(self) -> bool:
        return self._local.timed_out

    def poll(self) -> int | None:
        return self._local.poll()

    def communicate(
        self,
        input: str | None = None,
        timeout: float | None = None,
    ) -> tuple[str, str]:
        return self._local.communicate(input=input, timeout=timeout)

    def kill(self, sig: int | None = None) -> None:
        if sig is None:
            self._local.kill(None)
        else:
            # immediate (C1 kill switch): signal the host exec client, then attempt an
            # in-container kill as a fallback.
            self._local.kill(sig)
            self._kill_in_container(sig)

    # ------------------------------------------------------------------ internals

    def _current_container_pid(self) -> int | None:
        if self._container_pid_fn is not None:
            return self._container_pid_fn()
        return self._container_pid

    def _kill_in_container(self, sig: int | None) -> None:
        if self._docker_exec is None:
            return
        cpid = self._current_container_pid()
        if cpid is None:
            return
        try:
            self._docker_exec(self._container_name, ["kill", f"-{int(sig)}", str(cpid)])
        except Exception:
            pass
