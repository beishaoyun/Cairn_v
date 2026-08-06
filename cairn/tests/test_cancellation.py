"""TaskCancellation tests (owned by Agent 13).

First cancel records the reason, later cancels are idempotent; attach after cancel kills
immediately; cancel after attach kills the bound process; kill switch (C1) sends an
immediate SIGKILL without the SIGTERM→grace path.
"""
from __future__ import annotations

import signal
import threading

from cairn.dispatcher.runtime.cancellation import TaskCancellation


class FakeProc:
    """Minimal ExecProcess stand-in: records the signals sent to kill()."""

    def __init__(self):
        self.kills: list = []
        self.killed = False

    def kill(self, sig=None):
        self.kills.append(sig)
        self.killed = True

    def communicate(self, input=None, timeout=None):  # pragma: no cover
        return "", ""


def test_first_cancel_records_reason():
    c = TaskCancellation()
    assert not c.cancelled
    assert c.reason is None
    c.cancel("scope violation")
    assert c.cancelled
    assert c.reason == "scope violation"


def test_cancel_is_idempotent():
    c = TaskCancellation()
    c.cancel("first")
    c.cancel("second")
    c.cancel("third")
    assert c.reason == "first"


def test_attach_then_cancel_kills_process():
    c = TaskCancellation()
    p = FakeProc()
    c.attach_process(p)
    assert p.kills == []
    c.cancel("timeout")
    assert p.killed
    assert p.kills == [None]  # default (graceful) kill path


def test_attach_after_cancel_kills_immediately():
    c = TaskCancellation()
    c.cancel("already cancelled")
    p = FakeProc()
    c.attach_process(p)
    assert p.killed


def test_cancel_kills_all_attached_processes():
    c = TaskCancellation()
    p1, p2 = FakeProc(), FakeProc()
    c.attach_process(p1)
    c.attach_process(p2)
    c.cancel("kill all")
    assert p1.killed and p2.killed


def test_kill_switch_sends_immediate_sigkill():
    c = TaskCancellation()
    p = FakeProc()
    c.attach_process(p)
    c.kill_switch("C1 immediate")
    assert p.kills == [signal.SIGKILL]


def test_kill_switch_after_cancel_preserves_first_reason():
    c = TaskCancellation()
    c.cancel("graceful first")
    p = FakeProc()
    c.attach_process(p)
    assert p.killed  # already-cancelled attach path
    c.kill_switch("C1")  # no-op: reason already recorded
    assert c.reason == "graceful first"


def test_detach_process_is_safe():
    c = TaskCancellation()
    p = FakeProc()
    c.attach_process(p)
    c.detach_process(p)
    c.detach_process(p)  # double detach is a no-op
    c.cancel("after detach")
    assert not p.killed  # detached process is not killed by a later cancel


def test_concurrent_cancel_single_reason():
    c = TaskCancellation()
    threads = [
        threading.Thread(target=lambda i=i: c.cancel(f"r{i}"))
        for i in range(50)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert c.cancelled
    assert c.reason in {f"r{i}" for i in range(50)}
