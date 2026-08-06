"""Local execution backend tests (Agent 11).

Real host processes, no Docker. Covers the ``ExecutionBackend``/``ExecProcess``
protocol for ``runtime/local_backend.py`` + ``runtime/process.py``:
workspace dir creation, process-group isolation & SIGKILL cleanup, timeout →
``timed_out``, write_text_file + graph §4-15 path-traversal guard, and protocol
conformance.
"""
from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import pytest

from cairn.dispatcher.runtime.backend import ExecProcess, ExecutionBackend
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.process import LocalProcess, resolve_workspace_path


def _sleep_child(pid: int) -> bool:
    """Return True if the pid still exists (kill(pid, 0) succeeds)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _make_backend(tmp_path: Path, **kw) -> LocalBackend:
    return LocalBackend(
        None,
        workspace_root=str(tmp_path / "workspace"),
        completed_action=kw.pop("completed_action", "keep"),
    )


# --------------------------------------------------------------------------- protocol


def test_local_backend_implements_execution_backend_protocol():
    backend = _make_backend(Path("/tmp/unused"))
    assert isinstance(backend, ExecutionBackend)


def test_local_process_implements_exec_process_protocol(tmp_path):
    backend = _make_backend(tmp_path)
    backend.ensure_running("p1")
    proc = backend.build_exec_process(["echo", "hi"])
    assert isinstance(proc, ExecProcess)
    out, _ = proc.communicate()
    assert out.strip() == "hi"


# --------------------------------------------------------------------------- lifecycle


def test_ensure_running_creates_workspace_dir(tmp_path):
    backend = _make_backend(tmp_path)
    backend.ensure_running("proj_001")
    assert (Path(tmp_path) / "workspace" / "proj_001").is_dir()


def test_build_exec_process_communicate_returns_stdout(tmp_path):
    backend = _make_backend(tmp_path)
    backend.ensure_running("p1")
    proc = backend.build_exec_process(["echo", "hello world"])
    out, err = proc.communicate()
    assert out.strip() == "hello world"
    assert err == ""
    assert proc.poll() == 0


def test_stdout_stderr_separated(tmp_path):
    backend = _make_backend(tmp_path)
    backend.ensure_running("p1")
    proc = backend.build_exec_process(
        ["sh", "-c", "echo out-line; echo err-line >&2"]
    )
    out, err = proc.communicate()
    assert "out-line" in out
    assert "err-line" in err
    assert "err-line" not in out


def test_communicate_writes_stdin(tmp_path):
    backend = _make_backend(tmp_path)
    backend.ensure_running("p1")
    proc = backend.build_exec_process(["cat"])
    out, err = proc.communicate(input="hello via stdin\n")
    assert "hello via stdin" in out


# --------------------------------------------------------------------------- timeout


def test_communicate_timeout_sets_timed_out(tmp_path):
    backend = _make_backend(tmp_path)
    backend.ensure_running("p1")
    proc = backend.build_exec_process(["sleep", "60"], timeout=0.5)
    assert not proc.timed_out
    out, err = proc.communicate()
    assert proc.timed_out is True
    assert proc.poll() is not None  # terminated by the timeout path


def test_communicate_explicit_timeout_overrides(tmp_path):
    backend = _make_backend(tmp_path)
    backend.ensure_running("p1")
    proc = backend.build_exec_process(["sleep", "60"], timeout=30)
    out, err = proc.communicate(timeout=0.5)
    assert proc.timed_out is True


# --------------------------------------------------------------------------- kill


def test_kill_sigkill_is_immediate(tmp_path):
    backend = _make_backend(tmp_path)
    backend.ensure_running("p1")
    proc = backend.build_exec_process(["sleep", "60"])
    time.sleep(0.2)
    started = time.monotonic()
    proc.kill(signal.SIGKILL)
    while proc.poll() is None and time.monotonic() - started < 3:
        time.sleep(0.01)
    assert proc.poll() == -signal.SIGKILL


def test_process_group_isolated_and_killed(tmp_path):
    """start_new_session group isolation: SIGKILL reaps the whole group (C1)."""
    backend = _make_backend(tmp_path)
    backend.ensure_running("p1")
    child_file = tmp_path / "child.pid"
    proc = backend.build_exec_process(
        ["sh", "-c", f"sleep 30 & echo $! > {child_file}; wait"]
    )
    deadline = time.monotonic() + 3
    while not child_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    child_pid = int(child_file.read_text().strip())
    pgid = os.getpgid(proc.pid)  # session leader == process group leader

    proc.kill(signal.SIGKILL)
    # reap the leader
    deadline = time.monotonic() + 3
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.3)

    # the whole group must be gone within a short window (orphan cleanup)
    deadline = time.monotonic() + 3
    while _sleep_child(pgid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _sleep_child(pgid), "process group still alive after SIGKILL"
    assert not _sleep_child(child_pid), "child survived group SIGKILL"


# --------------------------------------------------------------------------- write_text_file


def test_write_text_file_creates_file(tmp_path):
    backend = _make_backend(tmp_path)
    backend.ensure_running("p1")
    backend.write_text_file("p1", "notes/scan.txt", "content-123")
    target = Path(tmp_path) / "workspace" / "p1" / "notes" / "scan.txt"
    assert target.read_text() == "content-123"


def test_write_text_file_accepts_absolute_container_path(tmp_path):
    backend = _make_backend(tmp_path)
    backend.ensure_running("p1")
    backend.write_text_file("p1", "/home/worker/workspace/a/b.txt", "x")
    assert (Path(tmp_path) / "workspace" / "p1" / "a" / "b.txt").read_text() == "x"


@pytest.mark.parametrize(
    "bad",
    [
        "../escape.txt",
        "sub/../../escape.txt",
        "a/./b.txt",
        "/etc/passwd/../escape",
        "a//b/..",
        "",
    ],
)
def test_write_text_file_rejects_traversal(tmp_path, bad):
    backend = _make_backend(tmp_path)
    backend.ensure_running("p1")
    with pytest.raises(ValueError):
        backend.write_text_file("p1", bad, "content")


def test_write_text_file_allows_dotfile(tmp_path):
    """`.hidden` is a legitimate filename — only `.`/`..` segments are blocked."""
    backend = _make_backend(tmp_path)
    backend.ensure_running("p1")
    backend.write_text_file("p1", ".hidden", "x")
    assert (Path(tmp_path) / "workspace" / "p1" / ".hidden").read_text() == "x"


def test_resolve_workspace_path_guard(tmp_path):
    ws = Path(tmp_path) / "workspace"
    good = resolve_workspace_path(ws, "p1", "notes/x.txt")
    assert good == (ws / "p1" / "notes" / "x.txt").resolve()
    with pytest.raises(ValueError):
        resolve_workspace_path(ws, "p1", "../outside")
    with pytest.raises(ValueError):
        resolve_workspace_path(ws, "p1", "a/./b")


# --------------------------------------------------------------------------- cleanup / close


def test_cleanup_managed_container_keep_is_noop(tmp_path):
    backend = _make_backend(tmp_path, completed_action="keep")
    backend.ensure_running("p1")
    backend.write_text_file("p1", "keep.txt", "x")
    backend.cleanup_managed_container("p1")
    assert (Path(tmp_path) / "workspace" / "p1" / "keep.txt").exists()


def test_cleanup_managed_container_remove_removes_workspace(tmp_path):
    backend = _make_backend(tmp_path, completed_action="remove")
    backend.ensure_running("p1")
    backend.write_text_file("p1", "gone.txt", "x")
    backend.cleanup_managed_container("p1")
    assert not (Path(tmp_path) / "workspace" / "p1").exists()


def test_close_is_noop(tmp_path):
    backend = _make_backend(tmp_path)
    backend.ensure_running("p1")
    backend.close()  # must not raise


def test_local_backend_does_not_inject_host_env_overrides(tmp_path):
    """Provided env is merged over os.environ, but never strips what the host needs."""
    backend = _make_backend(tmp_path)
    backend.ensure_running("p1")
    proc = backend.build_exec_process(["sh", "-c", "printf %s \"$MY_VAR\""], env={"MY_VAR": "zz"})
    out, _ = proc.communicate()
    assert out == "zz"
