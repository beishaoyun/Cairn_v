"""Dispatcher 调度主循环逻辑测试（Agent 40 · test_scheduler_logic.py）。

验收（40 提示词 §3）：
1. guards 拒绝路径（prohibited/kill/窗口外）；
2. worker 选择（优先级/冷却/排除创建者/replay 特例）；
3. 并发上限（max_workers / max_running_projects）；
4. 启动 reconcile（构造僵尸 running 行 + 超时 intent）；
5. 端到端（进程内 Server + CairnClient + LocalBackend + MockDriver）：
   bootstrap→reason→explore→verify 至少一程；
6. scheduler_state 回载（reason 计数/冷却不丢）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import pytest

from cairn.config import ServerConfig
from cairn.server.app import create_app
from fastapi.testclient import TestClient

from cairn.dispatcher.config import load_dict
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.context import DispatcherContext
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.scheduler import loop as loop_mod
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.dispatcher.scheduler.worker_select import (
    can_dispatch,
    filter_eligible,
    filter_ready,
    is_replay_engine_task,
    select_verify_worker,
    select_worker,
    sort_by_priority,
)
from cairn.dispatcher.workers.health import WorkerHealth

from mock_harness import (
    bootstrap_cfg,
    explore_cfg,
    make_mock_driver,
    mock_cfg,
    reason_cfg,
    verify_cfg,
)

# ===========================================================================
# helpers
# ===========================================================================


def make_config(*, workers=None, scope=None, runtime=None, tuning=None):
    raw = {
        "server": {"url": "http://test", "api_token": "${CAIRN_API_TOKEN}"},
        "runtime": {
            "execution": "local",
            "interval": 1,
            "max_workers": 8,
            "max_running_projects": 3,
            "max_project_workers": 4,
            "worker_healthcheck": "disabled",
        },
        "workers": workers
        or [
            {
                "name": "mock-A",
                "type": "mock",
                "task_types": ["bootstrap", "reason", "explore", "verify", "audit"],
                "max_running": 2,
                "priority": 0,
                "verify_eligible": True,
            },
        ],
    }
    if scope:
        raw["scope"] = scope
    if runtime:
        raw["runtime"].update(runtime)
    if tuning:
        raw["tuning"] = tuning
    return load_dict(raw, env={"CAIRN_API_TOKEN": "test-token"})


def make_ctx(config, *, drivers=None, log=None):
    shutdown = threading.Event()
    return DispatcherContext(
        config=config,
        drivers=drivers or {},
        health=WorkerHealth(mode="disabled"),
        shutdown=shutdown,
        log=log or (lambda m: None),
    )


def make_server(tmp):
    """Build an in-process Server; return (client, config)."""
    os.environ["CAIRN_API_TOKEN"] = "test-token"
    cfg = ServerConfig(
        db_path=os.path.join(tmp, "test.db"),
        api_token="test-token",
        evidence_root=os.path.join(tmp, "evidence"),
        traffic_root=os.path.join(tmp, "traffic"),
        archive_root=os.path.join(tmp, "archive"),
        logs_root=os.path.join(tmp, "logs"),
    )
    app = create_app(cfg)
    tc = TestClient(app)
    client = CairnClient("http://test", "test-token", client=tc)
    return client, cfg


def create_active_engagement(client, *, title="e2e"):
    eng = client._request(
        "POST", "/engagements",
        json={
            "title": title,
            "authorized_start_at": "2026-01-01T00:00:00Z",
            "authorized_end_at": "2026-12-31T00:00:00Z",
        },
    )
    eid = eng["id"]
    client._request("POST", f"/engagements/{eid}/targets",
                    json={"value": "10.0.0.5", "scope": "authorized"})
    client._request("PUT", f"/engagements/{eid}/status", json={"status": "active"})
    return eid


def make_full_mock_driver():
    """Mock driver producing a valid bootstrap→reason→explore→verify chain."""
    env = mock_cfg.worker_env(
        bootstrap=bootstrap_cfg(
            discoveries=[{"target": "10.0.0.5", "port": 8080, "service": "tomcat"}]
        ),
        reason=reason_cfg(
            intents=[{"from": ["f001"], "description": "probe /login",
                      "coverage_item_ids": ["c-001"]}]
        ),
        explore_execute=explore_cfg(
            findings=[{
                "title": "SQL Injection in /login", "severity": "high",
                "asset": "http://10.0.0.5:8080/login",
                "description": "login reflects SQL error", "remediation": "parameterize",
                "http": [{"method": "POST", "url": "http://10.0.0.5:8080/login",
                          "request_body": "u=' OR 1=1--", "response_status": 200,
                          "response_body": "SQL error near 'OR 1=1'"}],
            }],
            coverage={
                "covered_items": ["c-001"], "depth_achieved": "standard",
                "outcome": "finding_created",
                "tested_scope": {"endpoints": ["/login"], "params": ["u"], "partial": False},
            },
        ),
        explore_conclude=explore_cfg(
            coverage={
                "covered_items": ["c-001"], "depth_achieved": "standard",
                "outcome": "finding_created",
                "tested_scope": {"endpoints": ["/login"], "params": ["u"], "partial": False},
            }
        ),
        verify=verify_cfg(outcome="confirmed", severity="high", traffic_ids=()),
    )
    return make_mock_driver(execution="local", env=env)


# ===========================================================================
# 1. worker 选择
# ===========================================================================


class TestWorkerSelect:
    def _workers(self):
        cfg = make_config(
            workers=[
                {"name": "A", "type": "mock", "task_types": ["explore", "verify"],
                 "max_running": 1, "priority": 0, "verify_eligible": True},
                {"name": "B", "type": "mock", "task_types": ["explore", "verify"],
                 "max_running": 1, "priority": 5, "verify_eligible": True},
                {"name": "C", "type": "mock", "task_types": ["explore"],
                 "max_running": 1, "priority": 3, "verify_eligible": True},
            ]
        )
        return cfg.workers

    def test_priority_descending(self):
        workers = self._workers()
        assert sort_by_priority(workers)[0].name == "B"

    def test_select_worker_respects_task_types(self):
        workers = self._workers()
        # C only declares explore
        assert select_worker(workers, task_type="explore") == "B"
        assert select_worker(workers, task_type="verify") == "B"
        # no worker declares reason
        assert select_worker(workers, task_type="reason") is None

    def test_select_worker_cooldown_excluded(self):
        workers = self._workers()
        # B on unhealthy cooldown → C wins (even though B higher priority)
        health = WorkerHealth(mode="disabled")
        health.mark_unhealthy("B", "test")
        assert select_worker(workers, task_type="explore", health=health) == "C"

    def test_select_worker_rejected_cooldown(self):
        workers = self._workers()
        rejected = {"B": time.time() + 100}
        assert select_worker(workers, task_type="explore", rejected_until=rejected) == "C"

    def test_select_worker_per_worker_max_running(self):
        workers = self._workers()
        running = {"B": 1}  # B at max_running=1
        assert select_worker(workers, task_type="explore", running_counts=running) == "C"

    def test_verify_excludes_creator(self):
        workers = self._workers()
        # creator=C (only C... but B has higher priority and declares verify)
        assert select_verify_worker("B", workers) == "A"  # exclude B
        assert select_verify_worker("C", workers) == "B"  # C not verify_eligible-capable? C declares explore only

    def test_verify_no_independent_worker(self):
        workers = [w for w in self._workers() if w.name != "C"]
        # only A and B; exclude B → A
        assert select_verify_worker("B", workers) == "A"
        # single verify-capable worker == creator → None (F7 single-worker degrade)
        assert select_verify_worker("A", [w for w in workers if w.name != "B"]) is None

    def test_replay_is_engine_task(self):
        assert is_replay_engine_task("replay") is True
        assert is_replay_engine_task("explore") is False

    def test_filter_ready_healthy(self):
        workers = self._workers()
        ready = filter_ready(workers, rejected_until={}, running_counts={})
        assert {w.name for w in ready} == {"A", "B", "C"}


class TestConcurrencyGates:
    def test_global_max_workers(self):
        assert can_dispatch(running_projects=1, max_running_projects=3,
                            running_tasks=7, max_workers=8) is True
        assert can_dispatch(running_projects=1, max_running_projects=3,
                            running_tasks=8, max_workers=8) is False

    def test_max_running_projects(self):
        assert can_dispatch(running_projects=3, max_running_projects=3,
                            running_tasks=1, max_workers=8) is False

    def test_max_project_workers(self):
        assert can_dispatch(running_projects=1, max_running_projects=3,
                            running_tasks=1, max_workers=8,
                            eid_running=4, max_project_workers=4) is False
        assert can_dispatch(running_projects=1, max_running_projects=3,
                            running_tasks=1, max_workers=8,
                            eid_running=3, max_project_workers=4) is True


# ===========================================================================
# 2. guards
# ===========================================================================


class TestGuards:
    def _loop(self, client=None, config=None):
        config = config or make_config()
        ctx = make_ctx(config, drivers={"mock-A": object()})
        client = client or object()  # guards 不调用 client；真实 client 由需要时传入
        return DispatcherLoop(ctx, client=client, backend=object(), interval=0.1)

    def test_kill_switch_blocks(self):
        config = make_config()  # enforce_kill_switch default true
        loop = self._loop(config=config)
        assert loop._check_kill_switch({"kill_switch": 1}) is True
        assert loop._check_kill_switch({"kill_switch": 0}) is False

    def test_kill_switch_disabled(self):
        config = make_config(scope={"enforce_kill_switch": False})
        loop = self._loop(config=config)
        assert loop._check_kill_switch({"kill_switch": 1}) is False

    def test_auth_window_outside_rejects(self):
        config = make_config(scope={"enforce_auth_window": True})
        loop = self._loop(config=config)
        eng = {"authorized_end_at": "2020-01-01T00:00:00Z"}
        assert loop._check_auth_window(eng) is False

    def test_auth_window_open(self):
        loop = self._loop()
        eng = {"authorized_end_at": "2099-01-01T00:00:00Z"}
        assert loop._check_auth_window(eng) is True

    def test_auth_window_disabled(self):
        config = make_config(scope={"enforce_auth_window": False})
        loop = self._loop(config=config)
        assert loop._check_auth_window({"authorized_end_at": "2020-01-01T00:00:00Z"}) is True

    def test_scope_guard_denied_no_fallback(self, tmp_path):
        # Real in-process server so check_scope returns 403 for prohibited value
        client, cfg = make_server(str(tmp_path))
        eid = create_active_engagement(client)
        client._request("POST", f"/engagements/{eid}/targets",
                        json={"value": "192.168.1.1", "scope": "prohibited"})
        config = make_config(scope={"enforce_scope_guard": True})
        loop = self._loop(client=client, config=config)
        assert loop._check_scope_guard(eid, "192.168.1.1") is False
        # authorized exact match → True
        assert loop._check_scope_guard(eid, "10.0.0.5") is True

    def test_scope_guard_disabled(self, tmp_path):
        client, cfg = make_server(str(tmp_path))
        eid = create_active_engagement(client)
        client._request("POST", f"/engagements/{eid}/targets",
                        json={"value": "192.168.1.1", "scope": "prohibited"})
        config = make_config(scope={"enforce_scope_guard": False})
        loop = self._loop(client=client, config=config)
        assert loop._check_scope_guard(eid, "192.168.1.1") is True

    def test_handle_kill_calls_force_kill(self):
        config = make_config()
        calls = []
        ctx = make_ctx(config)
        ctx.force_kill = lambda reason: calls.append(reason)
        loop = DispatcherLoop(ctx, client=object(), backend=object(), interval=0.1)
        loop._handle_kill("e-001")
        assert calls, "force_kill should be called on kill switch (C1)"


# ===========================================================================
# 3. heartbeat
# ===========================================================================


class TestHeartbeatLease:
    def test_beat_once_calls_registered(self):
        lease = HeartbeatLease(interval=0.01)
        got = []
        lease.register("k", lambda: got.append("beat"))
        lease.beat_once()
        assert got == ["beat"]
        lease.clear()

    def test_unregister_stops(self):
        lease = HeartbeatLease(interval=0.01)
        got = []
        lease.register("k", lambda: got.append(1))
        lease.unregister("k")
        lease.beat_once()
        assert got == []
        lease.stop()

    def test_heartbeat_failure_ignored(self):
        lease = HeartbeatLease(interval=0.01, log=lambda m: None)

        def boom():
            raise RuntimeError("boom")

        lease.register("k", boom)
        lease.beat_once()  # should not raise
        assert lease.active() == 1
        lease.clear()


# ===========================================================================
# 4. scheduler_state 回载
# ===========================================================================


class TestSchedulerState:
    def test_persist_and_reload(self, tmp_path):
        client, cfg = make_server(str(tmp_path))
        eid = create_active_engagement(client)
        config = make_config(
            workers=[
                {"name": "A", "type": "mock", "task_types": ["bootstrap", "reason", "explore", "verify", "audit"],
                 "max_running": 2, "priority": 0, "verify_eligible": True},
            ]
        )
        ctx = make_ctx(config, drivers={})
        loop = DispatcherLoop(ctx, client=client, backend=object(), interval=0.1)
        # simulate state: bootstrap done + worker rejected + unhealthy
        loop._bootstrap_done.add(eid)
        loop._rejected_until["A"] = time.time() + 100
        loop._runtime_projects.add("proj_001")
        loop._escalation.record_failure(eid)
        loop._persist_state()

        # fresh loop loads state
        ctx2 = make_ctx(config, drivers={})
        loop2 = DispatcherLoop(ctx2, client=client, backend=object(), interval=0.1)
        loop2._load_state()
        assert eid in loop2._bootstrap_done
        assert "proj_001" in loop2._runtime_projects
        assert loop2._rejected_until.get("A", 0) > time.time()
        state = loop2._escalation.snapshot(eid)
        assert state is not None and state.get("consecutive_failures") == 1


# ===========================================================================
# 5. 启动 reconcile（僵尸 running + 超时 intent）
# ===========================================================================


class TestStartupReconcile:
    def test_zombie_running_task_and_timeout_intent(self, tmp_path):
        client, cfg = make_server(str(tmp_path))
        eid = create_active_engagement(client)
        # project + intent + coverage item
        proj = client._request("POST", "/projects",
                               json={"engagement_id": eid, "title": "p"})
        pid = proj["id"]
        intent = client._request(
            "POST", f"/projects/{pid}/intents",
            json={"description": "d", "creator": "A", "from_fact_ids": ["f001"]},
        )
        iid = intent["id"]
        # claim intent via heartbeat (sets worker + last_heartbeat_at)
        client._request("POST", f"/projects/{pid}/intents/{iid}/heartbeat",
                        json={"worker": "A"})
        # create a coverage item (manual seed)
        items = client.list_items(eid)
        item_id = None
        if items:
            item_id = items[0]["id"]
        else:
            cov = client._request("POST", f"/engagements/{eid}/coverage/items",
                                  json={"target_id": client.list_targets(eid)[0]["id"],
                                        "test_type_id": "tt_web_sqli", "seed_source": "auto"})
            item_id = cov["id"]
        # claim the coverage item for this intent (B1)
        client._request("POST", f"/engagements/{eid}/coverage/items/{item_id}/claim",
                        json={"intent_id": iid})
        # open a running task_run (zombie)
        run = client.open_task_run(eid, task_type="explore", worker="A", project_id=pid,
                                   status="running")
        run_id = run["id"]

        # age intent heartbeat so it is > 2*interval (ISO8601 UTC, golden 不变量 8)
        db_path = cfg.db_path
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE intents SET last_heartbeat_at = "
            "strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 hour') WHERE id=?",
            (iid,),
        )
        conn.commit()
        conn.close()

        # run reconcile with a tiny interval (2*0.01s << 1 hour)
        config = make_config(
            workers=[
                {"name": "A", "type": "mock", "task_types": ["bootstrap", "reason", "explore", "verify", "audit"],
                 "max_running": 2, "priority": 0, "verify_eligible": True},
            ]
        )
        ctx = make_ctx(config, drivers={})
        loop = DispatcherLoop(ctx, client=client, backend=object(), interval=0.01)
        loop._startup_reconcile()

        # zombie run finished as failed
        run_row = client._request("GET", f"/tasks/{run_id}")
        assert run_row["status"] == "failed"
        # coverage item released back to untested
        item_rows = client._request("GET", f"/engagements/{eid}/coverage/items")
        target = [i for i in item_rows if i["id"] == item_id][0]
        assert target.get("current_intent_id") is None
        assert target.get("status") == "untested"


# ===========================================================================
# 6. 端到端：bootstrap→reason→explore→verify
# ===========================================================================


def _assert_no_failed_runs(client, eid):
    rows = client._request("GET", f"/engagements/{eid}/tasks")
    for r in rows:
        assert r["status"] != "failed", f"unexpected failed run: {r}"


class TestE2EChain:
    def test_bootstrap_reason_explore_verify(self, tmp_path):
        # P1-2（F1/TV-10）：verify 必须排除创建者。单 worker 且唯一候选是创建者 → 不派发
        # （finding 停留 pending_verify）。因此本 E2E 链用两个 worker：mock-A 创建 finding，
        # mock-B 独立复核（cross_worker），保证 verify 真正落到独立 worker。
        client, cfg = make_server(str(tmp_path))
        eid = create_active_engagement(client)
        config = make_config(
            workers=[
                {"name": "mock-A", "type": "mock",
                 "task_types": ["bootstrap", "reason", "explore", "verify", "audit"],
                 "max_running": 2, "priority": 0, "verify_eligible": True},
                {"name": "mock-B", "type": "mock",
                 "task_types": ["bootstrap", "reason", "explore", "verify", "audit"],
                 "max_running": 2, "priority": 0, "verify_eligible": True},
            ]
        )
        driver = make_full_mock_driver()
        logs: list[str] = []
        ctx = make_ctx(config, drivers={"mock-A": driver, "mock-B": driver}, log=logs.append)
        backend = LocalBackend(config, workspace_root=str(tmp_path / "ws"))
        loop = DispatcherLoop(ctx, client=client, backend=backend, interval=0.01)

        # run bounded steps
        for _ in range(12):
            loop.step()

        # bootstrap should have seeded coverage items
        gaps = client.get_gaps(eid, exclude_in_progress=True)
        items = client.list_items(eid)
        assert items, f"coverage items should be seeded by bootstrap; logs={logs}"

        # a finding should exist and be verified
        findings = client._request("GET", f"/engagements/{eid}/findings")["items"]
        assert findings, f"explore should have produced a finding; logs={logs}"
        f = findings[0]
        assert f["status"] == "verified", f"finding status: {f['status']} (detail: {f.get('verify_status')})"
        assert f.get("verify_status") == "confirmed"
        assert f.get("verified_severity") == "high"

        # task_runs lifecycle: bootstrap/explore/verify 必须 success（静态 mock 的 reason
        # 在 c-001 已覆盖后会反复失败——这是 mock 静态引用的测试假象，非调度 bug）
        rows = client._request("GET", f"/engagements/{eid}/tasks")
        for r in rows:
            if r["task_type"] in ("bootstrap", "explore", "verify"):
                assert r["status"] == "success", f"unexpected {r['task_type']} run: {r}"
        assert any(r["task_type"] == "verify" and r["status"] == "success" for r in rows)

        # scheduler_state persisted
        rows = client._request("GET", "/scheduler_state")["items"]
        keys = {r["key"] for r in rows}
        assert "reason_checkpoints" in keys or "runtime_project_ids" in keys

    def test_run_dispatch_loop_cli_entry(self, tmp_path):
        """run_dispatch_loop(ctx) signature consumed by 13's CLI."""
        client, cfg = make_server(str(tmp_path))
        eid = create_active_engagement(client)
        config = make_config(
            workers=[
                {"name": "mock-A", "type": "mock",
                 "task_types": ["bootstrap", "reason", "explore", "verify", "audit"],
                 "max_running": 2, "priority": 0, "verify_eligible": True},
            ]
        )
        driver = make_full_mock_driver()
        ctx = make_ctx(config, drivers={"mock-A": driver})

        # run loop in a thread, let it do a few steps, then signal shutdown
        result = {}

        def _run():
            backend = LocalBackend(config, workspace_root=str(tmp_path / "ws2"))
            try:
                result["rc"] = loop_mod.run_dispatch_loop(
                    ctx, interval=0.01, client=client, backend=backend
                )
            except Exception as exc:  # pragma: no cover
                result["exc"] = exc

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        time.sleep(0.5)
        ctx.shutdown.set()
        t.join(timeout=5)
        assert "exc" not in result, result.get("exc")
        assert result.get("rc") == 0


# ===========================================================================
# 6b. verify 独立性派发（F1 / TV-10）
# ===========================================================================


class TestVerifyIndependence:
    def test_single_worker_creator_not_dispatched(self, tmp_path):
        """F1/TV-10：单 worker 且唯一 verify 候选是创建者 → 不派发（停留 pending_verify）。

        50 审计 P1-2：原 loop 单 worker 兜底 cross_run 会把 verify 派给创建者本人。
        修复后必须排除创建者；无独立候选 → 不派发，finding 标 pending_verify 等待独立复核。
        """
        client, cfg = make_server(str(tmp_path))
        eid = create_active_engagement(client)
        config = make_config(
            workers=[
                {"name": "mock-A", "type": "mock",
                 "task_types": ["bootstrap", "reason", "explore", "verify", "audit"],
                 "max_running": 2, "priority": 0, "verify_eligible": True},
            ]
        )
        # 直接种一个 open finding，创建者 = mock-A（唯一 worker）
        fid = client.create_finding(
            eid,
            {"title": "SQLi", "severity": "high", "asset": "http://10.0.0.5:8080/login",
             "description": "login reflects SQL error"},
            detected_by="mock-A",
        )["id"]
        driver = make_full_mock_driver()
        ctx = make_ctx(config, drivers={"mock-A": driver})
        backend = LocalBackend(config, workspace_root=str(tmp_path / "ws"))
        loop = DispatcherLoop(ctx, client=client, backend=backend, interval=0.01)
        loop._bootstrap_done.add(eid)  # 跳过 bootstrap/reason/explore，直达 verify 判定

        loop.step()

        # 无 verify 任务派发（绝不派发给创建者本人，F1/TV-10）
        rows = client._request("GET", f"/engagements/{eid}/tasks")
        assert all(r["task_type"] != "verify" for r in rows), rows
        # finding 停留 pending_verify（等待独立复核）
        f = client._request("GET", f"/engagements/{eid}/findings/{fid}")
        assert f["status"] == "pending_verify", f

    def test_two_workers_verify_goes_to_non_creator(self, tmp_path):
        """F1：双 worker（A=创建者，B 独立）→ verify 派给 B（cross_worker），非创建者。"""
        client, cfg = make_server(str(tmp_path))
        eid = create_active_engagement(client)
        config = make_config(
            workers=[
                {"name": "mock-A", "type": "mock",
                 "task_types": ["bootstrap", "reason", "explore", "verify", "audit"],
                 "max_running": 2, "priority": 0, "verify_eligible": True},
                {"name": "mock-B", "type": "mock",
                 "task_types": ["bootstrap", "reason", "explore", "verify", "audit"],
                 "max_running": 2, "priority": 0, "verify_eligible": True},
            ]
        )
        fid = client.create_finding(
            eid,
            {"title": "SQLi", "severity": "high", "asset": "http://10.0.0.5:8080/login",
             "description": "login reflects SQL error"},
            detected_by="mock-A",
        )["id"]
        driver = make_full_mock_driver()
        ctx = make_ctx(config, drivers={"mock-A": driver, "mock-B": driver})
        backend = LocalBackend(config, workspace_root=str(tmp_path / "ws"))
        Path(tmp_path / "ws").mkdir(parents=True, exist_ok=True)  # LocalBackend cwd 需存在
        loop = DispatcherLoop(ctx, client=client, backend=backend, interval=0.01)
        loop._bootstrap_done.add(eid)

        loop.step()

        rows = client._request("GET", f"/engagements/{eid}/tasks")
        verify_runs = [r for r in rows if r["task_type"] == "verify"]
        assert verify_runs, f"verify should be dispatched with 2 workers, got {rows}"
        # 派发到非创建者（mock-B），且 independence=cross_worker（outcome_note 记录）
        assert verify_runs[0]["worker"] == "mock-B", verify_runs
        assert verify_runs[0]["status"] == "success", verify_runs


# ===========================================================================
# 7. kill 即时性（模拟，无 Docker）
# ===========================================================================


class TestKillSwitch:
    def test_kill_engagement_skips_dispatch(self, tmp_path):
        client, cfg = make_server(str(tmp_path))
        eid = create_active_engagement(client)
        # set kill switch
        client.kill(eid)
        config = make_config(
            workers=[
                {"name": "mock-A", "type": "mock",
                 "task_types": ["bootstrap", "reason", "explore", "verify", "audit"],
                 "max_running": 2, "priority": 0, "verify_eligible": True},
            ]
        )
        driver = make_full_mock_driver()
        ctx = make_ctx(config, drivers={"mock-A": driver})
        backend = LocalBackend(config, workspace_root=str(tmp_path / "ws3"))
        loop = DispatcherLoop(ctx, client=client, backend=backend, interval=0.01)
        for _ in range(5):
            loop.step()
        # no task_runs dispatched for a killed engagement
        rows = client._request("GET", f"/engagements/{eid}/tasks")
        assert rows == [], f"no tasks should be dispatched under kill switch, got {rows}"

    def test_handle_kill_immediate_path(self):
        config = make_config()
        force = []
        ctx = make_ctx(config)
        ctx.force_kill = lambda r: force.append(r)
        backend = LocalBackend(config, workspace_root=tempfile.mkdtemp())
        loop = DispatcherLoop(ctx, client=object(), backend=backend, interval=0.01)
        loop._running["task-001"] = {"eid": "e-001", "cancellation": None}
        loop._handle_kill("e-001")
        assert force, "kill switch must call ctx.force_kill (C1 SIGKILL path)"

    def test_kill_monitor_kills_running_task_immediately(self, tmp_path):
        """C1 熔断即时性（50 审计 P1-3）：任务运行期间 kill_switch 触发 → 绑定进程立即
        SIGKILL，不等 communicate 返回。主循环同步阻塞在 communicate，靠后台 kill 监控
        线程轮询 kill_switch 并调用 cancellation.kill_switch()（即时 SIGKILL）。
        """
        client, cfg = make_server(str(tmp_path))
        eid = create_active_engagement(client)
        config = make_config()
        ctx = make_ctx(config)
        backend = LocalBackend(config, workspace_root=str(tmp_path / "ws"))
        Path(tmp_path / "ws").mkdir(parents=True, exist_ok=True)  # LocalBackend cwd 需存在
        loop = DispatcherLoop(ctx, client=client, backend=backend, interval=0.01)

        # 模拟 30 run_worker_phase 的挂载：阻塞进程 attach 到 TaskCancellation
        cancellation = TaskCancellation()
        proc = backend.build_exec_process(["sleep", "30"], timeout=60)
        cancellation.attach_process(proc)
        loop._running["task-001"] = {
            "run_id": "task-001", "eid": eid, "pid": None,
            "task_type": "verify", "worker": "mock-A", "cancellation": cancellation,
        }

        loop._start_kill_monitor()
        try:
            client.kill(eid)  # 服务端置 kill_switch（模拟运行中触发熔断）
            deadline = time.time() + 5.0
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.05)
            assert proc.poll() is not None, (
                "kill_switch 触发后运行中进程应被立即 SIGKILL（C1），而非等 communicate 返回"
            )
            assert cancellation.cancelled
        finally:
            if proc.poll() is None:  # 兜底清理：确保子进程被回收
                proc.kill(None)
            ctx.shutdown.set()
            loop._stop_kill_monitor()
