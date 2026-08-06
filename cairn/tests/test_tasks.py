"""Agent 30 验收测试：任务逻辑 + 契约校验器 + 写回 + replay + F9 流分类。

覆盖 dev-agents/30-dispatcher-tasks.md §3 验收点：
1. 每任务用（本地最小假驱动，31 mock 未就绪时 importorskip 保护 mock 用例）跑通；
2. 校验器单元测试：合法/非法 payload 各自拒绝；`complete` 字段被拒；verify 两阶段契约；
3. reason 收敛约束：覆盖未满不出 intent 也不出 finalize → 任务失败（C8 escalate）；
4. explore 写回：claim 互斥（他人格子被拒）、not_applicable 建议、traffic_ids 候选注入；
5. replay：remediated/unchanged 分支 + 账本幂等（TV-30/31/44）；
6. F9 分类：scanner 输出含 "error" 不产生 error 事件。

31（mock 驱动）未就绪时：本地最小假驱动（FakeDriver/FakeBackend/FakeClient）跑任务逻辑；
``test_mock_driver_full_chain`` 用 ``pytest.importorskip`` 保护（31 就绪后由 50 全量复验）。
"""

from __future__ import annotations

import json

import pytest

from cairn.dispatcher.config import (
    DispatcherConfig,
    RuntimeConfig,
    ServerConfig,
    TasksConfig,
    TuningConfig,
    WorkerConfig,
)
from cairn.dispatcher.errors import COVERAGE_ALREADY_COVERED, FINDING_DUP, CairnClientError
from cairn.dispatcher.findings.writer import FindingsWriter
from cairn.dispatcher.coverage.writer import CoverageWriter
from cairn.dispatcher.progress.stream import classify_line, summarize_event
from cairn.dispatcher.replay.engine import ReplayEngine
from cairn.dispatcher.tasks import (
    run_audit,
    run_bootstrap,
    run_explore,
    run_reason,
    run_verify,
    select_verify_worker,
)
from cairn.dispatcher.tasks.bootstrap import build_bootstrap_prompt
from cairn.dispatcher.tasks.common import (
    PayloadError,
    TaskContext,
    TaskResult,
    parse_accepted,
    validate_bootstrap_payload,
    validate_coverage_result,
    validate_explore_payload,
    validate_findings_payload,
    validate_reason_payload,
    validate_replay_result,
    validate_verify_blind_payload,
    validate_verify_compare_payload,
)
from cairn.dispatcher.tasks.explore import build_explore_prompt
from cairn.dispatcher.workers.base import WorkerCommand, WorkerDriver

# ---------------------------------------------------------------------------
# 本地最小假驱动 / 后端 / 客户端（31 mock 未就绪时的替代；直接调任务函数 + 假输入）
# ---------------------------------------------------------------------------


class FakeProcess:
    def __init__(self, stdout: str, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self._killed = False

    @property
    def pid(self) -> int | None:
        return 4242

    @property
    def timed_out(self) -> bool:
        return False

    def poll(self) -> int | None:
        return 0 if not self._killed else -9

    def communicate(self, input: str | None = None, timeout: float | None = None) -> tuple[str, str]:
        return (self.stdout, self.stderr)

    def kill(self, sig: int | None = None) -> None:
        self._killed = True


class FakeBackend:
    """顺序吐 canned stdout（两次调用 → verify blind/comparison 各一条）。"""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.build_calls: list[dict] = []

    def build_exec_process(self, command: list[str], **kw) -> FakeProcess:
        self.build_calls.append({"command": command, "kw": kw})
        stdout = self.responses.pop(0) if self.responses else ""
        return FakeProcess(stdout)


class FakeDriver(WorkerDriver):
    """本地假驱动：返回 canned JSON，记录执行过的 prompt。"""

    driver_type = "fake"
    local_binary = "echo"
    base_url_env = ""
    health_path = ""

    def __init__(self) -> None:
        super().__init__(execution="local")
        self.executed_prompts: list[str] = []

    def build_execute(self, prompt: str, *, session_id: str | None = None, **kw) -> WorkerCommand:
        self.executed_prompts.append(prompt)
        return WorkerCommand(argv=["fake-cli"], env={})

    def build_conclude(self, prompt: str, *, session_id: str | None = None, **kw) -> WorkerCommand:
        self.executed_prompts.append(prompt)
        return WorkerCommand(argv=["fake-cli"], env={})


class FakeResp:
    def __init__(self, status: int, body: bytes = b"ok", headers: dict | None = None) -> None:
        self.status_code = status
        self.content = body
        self.headers = headers or {}


class FakeHttp:
    def __init__(self, responses: list[FakeResp] | None = None) -> None:
        self.responses = list(responses or [])
        self.requests: list[tuple] = []

    def request(self, method: str, url: str, headers=None, content=None) -> FakeResp:
        self.requests.append((method, url))
        if self.responses:
            return self.responses.pop(0)
        return FakeResp(200)

    def close(self) -> None:
        pass


class FakeClient:
    """最小假客户端：记录调用 + 可配置端点 handler + 覆盖/finding/retest 账本。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, dict]] = []
        self.events: list[tuple[str, str, str, str]] = []
        self.coverage_data: dict = {"targets": [], "test_types": [], "cells": [], "summary": {}}
        self.traffic_data: list[dict] = []
        self.items: list[dict] = []
        self.gaps: list[dict] = []
        self.write_coverage_results: list[dict] = []
        self.created_findings: list[dict] = []
        self.retest_confirmations: list[dict] = []
        self.claim_results: dict[str, bool] = {}
        self._handlers: dict[str, callable] = {}

    def register(self, prefix: str, handler) -> None:
        self._handlers[prefix] = handler

    # ---- 底层 ----

    def _request(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None, **kw):
        json = json or {}
        params = params or {}
        self.calls.append((method, path, dict(json), dict(params)))
        for prefix, handler in self._handlers.items():
            if path.startswith(prefix):
                return handler(method, path, json=json, params=params, **kw)
        # B1 claim/release
        if path.endswith("/claim"):
            cid = path.split("/coverage/items/")[1].split("/")[0]
            return {"item_id": cid, "claimed": self.claim_results.get(cid, True)}
        if path.endswith("/release"):
            return {"released": True}
        # verify apply
        if "/findings/" in path and path.endswith("/verify"):
            return {"ok": True, "apply_verify_runs": json}
        # retest confirmation（幂等：同 fid+kind 只记一次）
        if "/findings/" in path and path.endswith("/retest"):
            key = (path.split("/findings/")[1].split("/")[0], json.get("kind"))
            if not any(c["fid"] == key[0] and c["kind"] == key[1] for c in self.retest_confirmations):
                self.retest_confirmations.append({"fid": key[0], "kind": key[1], **json})
            return {"retest_round": 1, "count": len([c for c in self.retest_confirmations if c["fid"] == key[0]])}
        # replay run 登记
        if "/findings/" in path and path.endswith("/replay"):
            return {"ok": True, "status": "queued"}
        # audit verdict
        if "/coverage/items/" in path and path.endswith("/audit"):
            return {"ok": True, **json}
        # coverage item seeding
        if method == "POST" and path.endswith("/coverage/items"):
            return {"ok": True, **json}
        # scheduler_state / reason escalation（路径假设，成功 no-op）
        return {"ok": True}

    # ---- 公共客户端方法（任务函数使用） ----

    def get_coverage(self, eid: str) -> dict:
        self.calls.append(("GET", f"/engagements/{eid}/coverage", {}, {}))
        return self.coverage_data

    def list_traffic(self, eid: str, *, client: str | None = None, since: str | None = None) -> list[dict]:
        self.calls.append(("GET", f"/engagements/{eid}/traffic", {}, {"client": client, "since": since}))
        return self.traffic_data

    def write_coverage_result(self, eid, *, item_ids, depth_achieved, outcome, fact_id, intent_id,
                              evidence_refs=None, tested_scope=None, partial=False, idempotency_key=None):
        rec = {
            "eid": eid, "item_ids": list(item_ids), "depth_achieved": depth_achieved,
            "outcome": outcome, "fact_id": fact_id, "intent_id": intent_id,
            "evidence_refs": list(evidence_refs or []), "tested_scope": tested_scope,
            "partial": partial, "idempotency_key": idempotency_key,
        }
        self.write_coverage_results.append(rec)
        return {"ok": True, "covered_items": list(item_ids)}

    def create_finding(self, eid, payload, *, detected_by=None, actor="agent"):
        self.created_findings.append(payload)
        return {"id": f"fd-{len(self.created_findings):03d}", "detected_by": detected_by, "actor": actor}

    def add_http_evidence(self, eid, fid, http_obj):
        self.calls.append(("POST", f"/engagements/{eid}/findings/{fid}/http", dict(http_obj), {}))
        return {"ok": True}

    def add_command_evidence(self, eid, fid, cmd):
        self.calls.append(("POST", f"/engagements/{eid}/findings/{fid}/commands", dict(cmd), {}))
        return {"ok": True}

    def link_traffic(self, eid, fid, traffic_ids, role, *, source=None):
        self.calls.append(("POST", f"/engagements/{eid}/findings/{fid}/traffic", {"traffic_ids": list(traffic_ids), "role": role, "source": source}, {}))
        return {"ok": True}

    def append_event(self, run_id, *, kind, level, message, raw_path=None):
        self.events.append((run_id, kind, level, message))
        return {"ok": True}

    def open_task_run(self, eid, *, task_type, worker, project_id=None):
        return {"id": f"task-{task_type}", "task_type": task_type, "worker": worker}

    def finish_task_run(self, run_id, status, *, outcome_note=None):
        return {"ok": True, "status": status}

    def list_items(self, eid):
        return self.items

    def get_gaps(self, eid, **kw):
        return self.gaps

    def check_scope(self, eid, value):
        return {"id": "t-001", "value": value}

    def create_target(self, eid, value, **kw):
        return {"id": "t-001", "value": value, **kw}

    def conclude_intent(self, pid, iid, *, worker, facts=None):
        return {"facts": [{"id": "f001", "description": (facts or [""])[0]}]}

    def resolve_traffic(self, eid, tid, *, for_model=True):
        if for_model:
            return {"id": tid, "mode": "digest", "digest": f"digest:{tid}", "url": "http://x/", "method": "GET", "status": 200}
        return {
            "id": tid, "mode": "full",
            "request": "GET /login HTTP/1.1\r\nHost: x\r\n\r\n",
            "response": "HTTP/1.1 200 OK\r\n\r\nok",
            "url": "http://x/login", "method": "GET", "status": 200,
        }


def make_config() -> DispatcherConfig:
    return DispatcherConfig(
        server=ServerConfig(url="http://cairn-server", api_token="test-token"),
        runtime=RuntimeConfig(execution="local"),
        workers=[WorkerConfig(name="worker-a", type="mock", task_types=["bootstrap", "reason", "explore", "verify", "audit"])],
        tasks=TasksConfig(),
        tuning=TuningConfig(),
    )


def make_ctx(client=None, *, eid="e-001", worker="worker-a", project_id="p-001") -> TaskContext:
    client = client or FakeClient()
    return TaskContext(
        client=client,
        config=make_config(),
        worker=worker,
        eid=eid,
        project_id=project_id,
        run_id="task-001",
    )


# ---------------------------------------------------------------------------
# 校验器单元测试（验收点 2）
# ---------------------------------------------------------------------------


class TestValidators:
    def test_bootstrap_valid(self):
        data = validate_bootstrap_payload({
            "accepted": True,
            "data": {
                "fact": {"description": "found 80/8080"},
                "sweep_complete": {"description": "initial sweep done"},
                "discoveries": [{"target": "10.0.0.5", "port": 80, "service": "http"}],
                "coverage": {"outcome": "no_issue"},
            },
        })
        assert data["discoveries"][0]["target"] == "10.0.0.5"

    def test_bootstrap_rejects_complete(self):
        with pytest.raises(PayloadError, match="complete"):
            validate_bootstrap_payload({
                "accepted": True,
                "data": {
                    "fact": {"description": "x"},
                    "complete": {"description": "done"},
                    "discoveries": [],
                },
            })

    def test_bootstrap_rejects_missing_fact(self):
        with pytest.raises(PayloadError):
            validate_bootstrap_payload({"accepted": True, "data": {"discoveries": []}})

    def test_reason_valid_intents(self):
        data = validate_reason_payload(
            {
                "accepted": True,
                "data": {
                    "intents": [{"from": ["f001"], "description": "test sqli", "coverage_item_ids": ["c-013"]}],
                    "coverage": {"recommend_finalize": False, "reason": ""},
                },
            },
            gap_item_ids=["c-013"],
            valid_fact_ids=["f001"],
            high_priority_gaps=True,
        )
        assert data["intents"][0]["coverage_item_ids"] == ["c-013"]

    def test_reason_convergence_fails_when_unmet(self):
        """覆盖未满：高优先缺口存在，无 intent 无 finalize → 校验失败。"""
        with pytest.raises(PayloadError, match="收敛约束"):
            validate_reason_payload(
                {
                    "accepted": True,
                    "data": {"intents": [], "coverage": {"recommend_finalize": False, "reason": ""}},
                },
                gap_item_ids=["c-013"],
                high_priority_gaps=True,
            )

    def test_reason_low_priority_gaps_allow_empty(self):
        """无高优先缺口 → 空 intents 且无 finalize 合法（收敛达标）。"""
        data = validate_reason_payload(
            {"accepted": True, "data": {"intents": [], "coverage": {"recommend_finalize": False, "reason": "low value"}}},
            gap_item_ids=["c-013"],
            high_priority_gaps=False,
        )
        assert data["intents"] == []

    def test_reason_rejects_out_of_gap_item(self):
        with pytest.raises(PayloadError, match="未覆盖范围之外"):
            validate_reason_payload(
                {"accepted": True, "data": {"intents": [{"from": ["f001"], "description": "x", "coverage_item_ids": ["c-999"]}]}},
                gap_item_ids=["c-013"],
            )

    def test_reason_rejects_complete(self):
        with pytest.raises(PayloadError, match="complete"):
            validate_reason_payload(
                {"accepted": True, "data": {"intents": [], "coverage": {"complete": True}}},
                gap_item_ids=["c-013"],
                high_priority_gaps=True,
            )

    def test_reason_waiver_kind_whitelist(self):
        with pytest.raises(PayloadError, match="kind"):
            validate_reason_payload(
                {"accepted": True, "data": {
                    "intents": [], "coverage": {"recommend_finalize": True, "reason": "ok",
                                                 "waivers": [{"item_id": "c-013", "kind": "bogus", "reason": "r"}]},
                }},
                gap_item_ids=["c-013"],
                high_priority_gaps=True,
            )

    def test_explore_valid(self):
        data = validate_explore_payload(
            {
                "accepted": True,
                "data": {
                    "description": "tested /admin",
                    "findings": [{"title": "Default creds", "severity": "high", "cvss_score": 8.1,
                                  "cwe_id": "CWE-521", "asset": "http://x/admin",
                                  "evidence_refs": ["e-001/x.png"]}],
                    "coverage": {"covered_items": ["c-013"], "depth_achieved": "standard",
                                 "outcome": "finding_created", "tested_scope": {"endpoints": ["/admin"], "partial": False}},
                },
            },
            known_item_ids=["c-013"],
        )
        assert data["findings"][0]["severity"] == "high"

    def test_explore_missing_coverage_fails(self):
        with pytest.raises(PayloadError, match="coverage"):
            validate_explore_payload(
                {"accepted": True, "data": {"description": "x", "findings": []}},
            )

    def test_explore_rejects_complete(self):
        with pytest.raises(PayloadError, match="complete"):
            validate_explore_payload(
                {"accepted": True, "data": {"description": "x", "complete": {}, "findings": [],
                                            "coverage": {"covered_items": ["c-013"], "depth_achieved": "standard", "outcome": "no_issue"}}},
                known_item_ids=["c-013"],
            )

    def test_explore_cross_check_findings_require_finding_created(self):
        with pytest.raises(PayloadError, match="finding_created"):
            validate_explore_payload(
                {"accepted": True, "data": {
                    "description": "x",
                    "findings": [{"title": "F", "severity": "low", "asset": "http://x"}],
                    "coverage": {"covered_items": ["c-013"], "depth_achieved": "standard", "outcome": "no_issue",
                                 "tested_scope": {"endpoints": ["/x"], "partial": False}},
                }},
                known_item_ids=["c-013"],
            )

    def test_findings_payload_severity_whitelist(self):
        with pytest.raises(PayloadError, match="severity"):
            validate_findings_payload([{"title": "x", "severity": "insane", "asset": "http://x"}])

    def test_findings_payload_cvss_range(self):
        with pytest.raises(PayloadError, match="cvss"):
            validate_findings_payload([{"title": "x", "severity": "high", "cvss_score": 11, "asset": "http://x"}])

    def test_findings_payload_cwe_format(self):
        with pytest.raises(PayloadError, match="CWE"):
            validate_findings_payload([{"title": "x", "severity": "high", "cwe_id": "CVE-2024-1", "asset": "http://x"}])

    def test_findings_payload_evidence_relpath(self):
        with pytest.raises(PayloadError, match="evidence_refs"):
            validate_findings_payload([{"title": "x", "severity": "high", "asset": "http://x",
                                        "evidence_refs": ["/etc/passwd"]}])

    def test_coverage_result_no_issue_requires_tested_scope(self):
        with pytest.raises(PayloadError, match="tested_scope"):
            validate_coverage_result({"covered_items": ["c-013"], "depth_achieved": "standard", "outcome": "no_issue"},
                                     known_item_ids=["c-013"])

    def test_coverage_result_not_applicable_valid(self):
        data = validate_coverage_result(
            {"covered_items": ["c-013"], "depth_achieved": "standard", "outcome": "not_applicable",
             "tested_scope": None},
            known_item_ids=["c-013"],
        )
        assert data["outcome"] == "not_applicable"

    def test_verify_blind_valid(self):
        data = validate_verify_blind_payload(
            {"accepted": True, "data": {"observations": [{"vuln": "sqli", "severity": "high", "traffic_id": "tr-001"}]}}
        )
        assert data["observations"][0]["vuln"] == "sqli"

    def test_verify_blind_empty_observations_valid(self):
        """诚实负面合法（prompts §4.1：observations: [] 是合法答案）。"""
        data = validate_verify_blind_payload({"accepted": True, "data": {"observations": [], "traffic_note": "none"}})
        assert data["observations"] == []

    def test_verify_blind_missing_observations_fails(self):
        with pytest.raises(PayloadError, match="observations"):
            validate_verify_blind_payload({"accepted": True, "data": {"traffic_note": "x"}})

    def test_verify_compare_valid(self):
        data = validate_verify_compare_payload(
            {"accepted": True, "data": {"stage": "comparison", "verdict": "confirmed",
                                        "verified_severity": "high", "reason": "ok",
                                        "verified_traffic_ids": ["tr-001"], "http_mismatch": False}},
            traffic_ids=["tr-001"],
        )
        assert data["verdict"] == "confirmed"

    def test_verify_compare_stage_required(self):
        with pytest.raises(PayloadError, match="stage"):
            validate_verify_compare_payload(
                {"accepted": True, "data": {"verdict": "confirmed", "verified_severity": "high", "reason": "r"}}
            )

    def test_verify_compare_verdict_enum(self):
        with pytest.raises(PayloadError, match="verdict"):
            validate_verify_compare_payload(
                {"accepted": True, "data": {"stage": "comparison", "verdict": "maybe", "verified_severity": "high", "reason": "r"}}
            )

    def test_verify_compare_traffic_must_exist(self):
        with pytest.raises(PayloadError, match="不存在"):
            validate_verify_compare_payload(
                {"accepted": True, "data": {"stage": "comparison", "verdict": "confirmed",
                                            "verified_severity": "high", "reason": "r", "verified_traffic_ids": ["tr-999"]}},
                traffic_ids=["tr-001"],
            )

    def test_replay_result_valid(self):
        assert validate_replay_result({"matched_original": 0, "result": "remediated"})["result"] == "remediated"

    def test_replay_result_enum(self):
        with pytest.raises(PayloadError, match="result"):
            validate_replay_result({"matched_original": 0, "result": "bogus"})

    def test_accepted_false_rejected(self):
        with pytest.raises(Exception, match="mock_rejected"):
            parse_accepted({"accepted": False, "reason": "mock_rejected"})

    def test_complete_rejected_nested_in_finding(self):
        """findings 内嵌套 complete 同样被拒（递归拒绝）。"""
        with pytest.raises(PayloadError, match="complete"):
            validate_findings_payload(
                [{"title": "x", "severity": "high", "asset": "http://x", "complete": {"description": "y"}}]
            )


# ---------------------------------------------------------------------------
# reason 任务（验收点 3：收敛约束 → 任务失败 + escalate）
# ---------------------------------------------------------------------------


class TestReasonTask:
    def _reason_result(self, output: str, gaps=None):
        client = FakeClient()
        ctx = make_ctx(client)
        driver = FakeDriver()
        backend = FakeBackend([output])
        gaps = gaps or [
            {"item_id": "c-013", "target_id": "t-001", "target_value": "10.0.0.5",
             "test_type_id": "tt_web_sqli", "test_type_name": "SQL注入", "depth": "deep", "priority": 0.9}
        ]
        return run_reason(ctx, driver=driver, backend=backend, gaps=gaps, graph_yaml="id: f001\n  description: recon")

    def test_success_intents(self):
        result = self._reason_result(
            '{"accepted": true, "data": {"intents": [{"from": ["f001"], "description": "test sqli", '
            '"coverage_item_ids": ["c-013"]}], "coverage": {"recommend_finalize": false, "reason": ""}}}'
        )
        assert result.status == "success"
        assert result.data["intents"][0]["coverage_item_ids"] == ["c-013"]

    def test_convergence_failure_escalates(self):
        """覆盖未满且不出 intent 也不出 finalize → 任务失败 + escalate（C8）。"""
        result = self._reason_result(
            '{"accepted": true, "data": {"intents": [], "coverage": {"recommend_finalize": false, "reason": ""}}}'
        )
        assert result.status == "failed"
        assert result.escalate is True
        assert result.error_code == "REASON_CONVERGENCE"

    def test_no_high_priority_gap_allows_empty(self):
        result = self._reason_result(
            '{"accepted": true, "data": {"intents": [], "coverage": {"recommend_finalize": false, "reason": "low"}}}',
            gaps=[{"item_id": "c-013", "target_id": "t-001", "target_value": "10.0.0.5",
                   "test_type_id": "tt_web_sqli", "test_type_name": "SQL注入", "depth": "deep", "priority": 0.1}],
        )
        assert result.status == "success"

    def test_complete_rejected_in_task(self):
        result = self._reason_result(
            '{"accepted": true, "data": {"intents": [], "complete": {"description": "x"}, "coverage": {}}}'
        )
        assert result.status == "failed"

    def test_reason_prompt_contains_gaps(self):
        driver = FakeDriver()
        ctx = make_ctx(FakeClient())
        run_reason(ctx, driver=driver, backend=FakeBackend([
            '{"accepted": true, "data": {"intents": [{"from": ["f001"], "description": "x", "coverage_item_ids": ["c-013"]}], "coverage": {"recommend_finalize": false}}}'
        ]), gaps=[{"item_id": "c-013", "priority": 0.9, "target_value": "10.0.0.5", "target_id": "t-001",
                   "test_type_id": "tt_web_sqli", "test_type_name": "SQL注入", "depth": "deep"}], graph_yaml="id: f001")
        assert "c-013" in driver.executed_prompts[0]

    def test_reason_escalation_tracker(self):
        """C8：连续失败超限 → escalated=True；finalize 被拒同样计数。"""
        from cairn.dispatcher.tasks.reason import ReasonEscalation

        esc = ReasonEscalation(max_consecutive_failures=3, max_finalize_rejected=3)
        for _ in range(3):
            st = esc.record_failure("e-001")
        assert st["escalated"] is True
        assert esc.snapshot("e-001")["consecutive_failures"] == 3
        esc.reset("e-001")
        st2 = esc.record_failure("e-001", finalize_rejected=True)
        st2 = esc.record_failure("e-001", finalize_rejected=True)
        st2 = esc.record_failure("e-001", finalize_rejected=True)
        assert st2["escalated"] is True


# ---------------------------------------------------------------------------
# bootstrap 任务
# ---------------------------------------------------------------------------


class TestBootstrapTask:
    def test_success_with_seeding(self):
        client = FakeClient()
        client.coverage_data = {
            "targets": [{"id": "t-001", "value": "10.0.0.5", "criticality": 0.8}],
            "test_types": [{"id": "tt_web_sqli", "name": "SQL注入", "category": "webapp", "risk": 0.9}],
            "cells": [],
            "summary": {},
        }
        ctx = make_ctx(client)
        driver = FakeDriver()
        backend = FakeBackend([
            '{"accepted": true, "data": {"fact": {"description": "found 80/8080"}, '
            '"sweep_complete": {"description": "done"}, '
            '"discoveries": [{"target": "10.0.0.5", "port": 8080, "service": "tomcat"}], '
            '"coverage": {"outcome": "no_issue"}}}'
        ])
        result = run_bootstrap(ctx, driver=driver, backend=backend, origin="scope", goal="sweep")
        assert result.status == "success"
        assert result.data["sweep_complete"]
        assert len(result.data["discoveries"]) == 1

    def test_complete_rejected(self):
        ctx = make_ctx(FakeClient())
        backend = FakeBackend([
            '{"accepted": true, "data": {"fact": {"description": "x"}, "complete": {"description": "done"}, "discoveries": []}}'
        ])
        result = run_bootstrap(ctx, driver=FakeDriver(), backend=backend, origin="o", goal="g")
        assert result.status == "failed"

    def test_bootstrap_prompt_has_no_complete(self):
        prompt = build_bootstrap_prompt(origin="o", goal="g", scope="s")
        # 输出契约字段用 sweep_complete（初探完成），不定义 complete 字段
        assert '"complete":' not in prompt
        assert "sweep_complete" in prompt


# ---------------------------------------------------------------------------
# explore 任务（验收点 4：claim 互斥 / not_applicable 建议 / traffic_ids 候选注入）
# ---------------------------------------------------------------------------


def _explore_output(outcome="finding_created", findings=None, depth="standard", tested_scope=None):
    findings = findings if findings is not None else []
    tested_scope = tested_scope if tested_scope is not None else {"endpoints": ["/admin"], "partial": False}
    return {
        "accepted": True,
        "data": {
            "description": "tested /admin",
            "findings": findings,
            "coverage": {"covered_items": ["c-013"], "depth_achieved": depth,
                         "outcome": outcome, "tested_scope": tested_scope},
        },
    }


class TestExploreTask:
    def _client_with_cell(self):
        client = FakeClient()
        client.coverage_data = {
            "targets": [{"id": "t-001", "value": "10.0.0.5", "criticality": 0.8}],
            "test_types": [{"id": "tt_web_sqli", "name": "SQL注入", "category": "webapp", "risk": 0.9}],
            "cells": [{"item_id": "c-013", "target_id": "t-001", "test_type_id": "tt_web_sqli",
                       "depth_required": "deep", "status": "in_progress"}],
            "summary": {},
        }
        client.traffic_data = [{"id": "tr-001", "method": "POST", "url": "http://x/login", "client": "worker-a"}]
        return client

    def test_success_writeback(self):
        client = self._client_with_cell()
        ctx = make_ctx(client)
        intent = {"id": "i-001", "description": "test sqli", "coverage_item_ids": ["c-013"], "from_fact_ids": ["f001"]}
        import json as _json
        backend = FakeBackend([_json.dumps(_explore_output(
            outcome="finding_created",
            findings=[{"title": "SQLi", "severity": "high", "cvss_score": 8.1, "cwe_id": "CWE-89",
                       "asset": "http://x/login", "traffic_ids": ["tr-001"],
                       "http": [{"method": "POST", "url": "http://x/login", "response_status": 200}],
                       "evidence_refs": ["e-001/x.png"]}],
        ))])
        result = run_explore(ctx, driver=FakeDriver(), backend=backend, intent=intent, graph_yaml="id: f001")
        assert result.status == "success"
        assert client.write_coverage_results[0]["outcome"] == "finding_created"
        assert client.write_coverage_results[0]["intent_id"] == "i-001"
        # findings 落库
        assert len(client.created_findings) == 1
        assert client.created_findings[0]["title"] == "SQLi"
        # 幂等键 = item_id:intent_id
        assert client.write_coverage_results[0]["idempotency_key"] == "c-013:i-001"
        # traffic_ids 候选注入：list_traffic 以 worker 检索
        assert any(c[0] == "GET" and c[1].endswith("/traffic") for c in client.calls)

    def test_claim_mutex_rejects_foreign_cell(self):
        """他人格子被拒（claim 返回 false）→ 不派发，busy 返回，写回不执行。"""
        client = self._client_with_cell()
        client.claim_results = {"c-013": False}  # 他人已认领
        ctx = make_ctx(client)
        intent = {"id": "i-001", "description": "x", "coverage_item_ids": ["c-013"], "from_fact_ids": ["f001"]}
        backend = FakeBackend([])  # 不应消费模型调用
        result = run_explore(ctx, driver=FakeDriver(), backend=backend, intent=intent, graph_yaml="id: f001")
        assert result.status == "failed"
        assert result.error_code == COVERAGE_ALREADY_COVERED
        assert result.extra.get("claimed") is False
        assert backend.build_calls == []  # 未派发

    def test_not_applicable_only_suggests(self):
        """outcome=not_applicable：只建议不置状态（B4），写回 success。"""
        client = self._client_with_cell()
        ctx = make_ctx(client)
        intent = {"id": "i-001", "description": "x", "coverage_item_ids": ["c-013"], "from_fact_ids": ["f001"]}
        import json as _json
        backend = FakeBackend([_json.dumps(_explore_output(outcome="not_applicable", tested_scope=None))])
        result = run_explore(ctx, driver=FakeDriver(), backend=backend, intent=intent, graph_yaml="id: f001")
        assert result.status == "success"
        assert client.write_coverage_results[0]["outcome"] == "not_applicable"
        # not_applicable 不置状态由服务端 B4 语义落实；Dispatcher 侧写回成功即满足
        assert result.data["coverage"]["outcome"] == "not_applicable"

    def test_traffic_candidates_injected_in_prompt(self):
        """traffic_ids 候选注入 explore prompt（C5：Agent 只从候选引用）。"""
        prompt = build_explore_prompt(
            graph_yaml="id: f001", intent_id="i-001", intent_description="x",
            coverage_context=[{"item_id": "c-013", "target_value": "10.0.0.5"}],
            traffic_candidates=[{"id": "tr-001", "method": "POST", "url": "http://x/login"}],
        )
        assert "tr-001" in prompt
        assert "coverage" in prompt
        assert "c-013" in prompt

    def test_explore_prompt_rejects_complete_field(self):
        prompt = build_explore_prompt(
            graph_yaml="id: f001", intent_id="i-001", intent_description="x",
            coverage_context=[], traffic_candidates=[],
        )
        assert '"complete":' not in prompt


# ---------------------------------------------------------------------------
# verify 任务（两阶段 + 派发选择）
# ---------------------------------------------------------------------------


class TestVerifyTask:
    def test_verify_worker_selection_excludes_creator(self):
        from cairn.dispatcher.config import WorkerConfig

        workers = [
            WorkerConfig(name="worker-A", type="mock", task_types=["verify"], verify_eligible=True),
            WorkerConfig(name="worker-B", type="mock", task_types=["verify"], verify_eligible=True),
        ]
        assert select_verify_worker("worker-A", workers) == "worker-B"
        assert select_verify_worker("worker-B", workers) == "worker-A"

    def test_verify_worker_selection_skips_ineligible(self):
        from cairn.dispatcher.config import WorkerConfig

        workers = [
            WorkerConfig(name="worker-A", type="mock", task_types=["verify"], verify_eligible=True),
            WorkerConfig(name="worker-B", type="mock", task_types=["verify"], verify_eligible=False),
        ]
        assert select_verify_worker("worker-A", workers) is None  # 仅创建者 + 不可复核

    def test_verify_full_chain_confirmed(self):
        client = FakeClient()
        ctx = make_ctx(client, worker="worker-B")
        finding = {"id": "fd-001", "title": "SQLi", "severity": "high",
                   "traffic_links": [{"traffic_id": "tr-001", "role": "trigger"}],
                   "detected_by": "worker-A"}
        blind = '{"accepted": true, "data": {"observations": [{"vuln": "SQLi", "severity": "high"}], "traffic_note": ""}}'
        cmp = '{"accepted": true, "data": {"stage": "comparison", "verdict": "confirmed", "verified_severity": "high", "reason": "ok", "verified_traffic_ids": ["tr-001"], "http_mismatch": false}}'
        result = run_verify(ctx, driver=FakeDriver(), backend=FakeBackend([blind, cmp]),
                            finding=finding, eid="e-001")
        assert result.status == "success"
        assert result.data["verdict"] == "confirmed"
        assert result.data["independence"] == "cross_worker"
        # apply_verify_runs 落定
        assert any(c[1].endswith("/verify") for c in client.calls)

    def test_verify_http_mismatch_downgrades_to_needs_more(self):
        """C2：盲审 observations 与 claim 一致，但 http[] 与捕获字节不符 → needs_more。"""
        client = FakeClient()
        ctx = make_ctx(client, worker="worker-B")
        # finding 的 http_evidence claim 与 resolve_traffic 捕获不符（URL 不同）
        finding = {"id": "fd-001", "title": "SQLi", "severity": "high",
                   "http_evidence": [{"traffic_id": "tr-001", "method": "POST",
                                      "url": "http://evil.example/login", "response_status": 200}]}
        blind = '{"accepted": true, "data": {"observations": [{"vuln": "SQLi"}], "traffic_note": ""}}'
        cmp = '{"accepted": true, "data": {"stage": "comparison", "verdict": "confirmed", "verified_severity": "high", "reason": "ok", "verified_traffic_ids": ["tr-001"], "http_mismatch": false}}'
        result = run_verify(ctx, driver=FakeDriver(), backend=FakeBackend([blind, cmp]),
                            finding=finding, eid="e-001")
        assert result.status == "success"
        assert result.data["http_mismatch"] is True
        assert result.data["verdict"] == "needs_more_evidence"

    def test_verify_cross_model_independence(self):
        client = FakeClient()
        ctx = make_ctx(client, worker="worker-B")
        finding = {"id": "fd-001", "title": "SQLi", "severity": "high", "traffic_links": []}
        blind = '{"accepted": true, "data": {"observations": [], "traffic_note": ""}}'
        cmp = '{"accepted": true, "data": {"stage": "comparison", "verdict": "confirmed", "verified_severity": "high", "reason": "ok", "verified_traffic_ids": [], "http_mismatch": false}}'
        result = run_verify(ctx, driver=FakeDriver(), backend=FakeBackend([blind, cmp]),
                            finding=finding, eid="e-001", verify_policy={"verify_model": "deepseek-v4"})
        assert result.data["independence"] == "cross_model"


# ---------------------------------------------------------------------------
# audit 任务
# ---------------------------------------------------------------------------


class TestAuditTask:
    def test_audit_match(self):
        client = FakeClient()
        ctx = make_ctx(client)
        item = {"id": "c-013", "target_id": "t-001", "target_value": "10.0.0.5",
                "test_type_id": "tt_web_sqli", "test_type_name": "SQL注入",
                "depth_required": "standard", "status": "tested_no_issue"}
        out = '{"accepted": true, "data": {"description": "re-test ok", "findings": [], "coverage": {"covered_items": ["c-013"], "depth_achieved": "standard", "outcome": "no_issue", "tested_scope": {"endpoints": [], "partial": false}}, "verdict": "match"}}'
        result = run_audit(ctx, driver=FakeDriver(), backend=FakeBackend([out]), item=item)
        assert result.status == "success"
        assert result.data["verdict"] == "match"

    def test_audit_discrepancy(self):
        client = FakeClient()
        ctx = make_ctx(client)
        item = {"id": "c-013", "target_id": "t-001", "target_value": "10.0.0.5",
                "test_type_id": "tt_web_sqli", "test_type_name": "SQL注入",
                "depth_required": "standard", "status": "tested_with_finding"}
        out = '{"accepted": true, "data": {"description": "no finding reproducible", "findings": [], "coverage": {"covered_items": ["c-013"], "depth_achieved": "standard", "outcome": "no_issue", "tested_scope": {"endpoints": [], "partial": false}}, "verdict": "coverage_discrepancy"}}'
        result = run_audit(ctx, driver=FakeDriver(), backend=FakeBackend([out]), item=item)
        assert result.status == "success"
        assert result.data["verdict"] == "coverage_discrepancy"


# ---------------------------------------------------------------------------
# replay 引擎（验收点 5：remediated/unchanged + 账本幂等）
# ---------------------------------------------------------------------------


class TestReplayEngine:
    def test_compare_signature_pure(self):
        eng = ReplayEngine(client=None)
        m = eng.compare_signature({"status": 200, "body": b"SQL error"}, {"status": 200, "body": b"SQL error"})
        assert m["matched"] is True
        m2 = eng.compare_signature({"status": 200, "body": b"ok"}, {"status": 200, "body": b"SQL error"})
        assert m2["matched"] is False and m2["status_match"] is True and m2["body_match"] is False
        m3 = eng.compare_signature({"status": 403, "body": b"forbidden"}, {"status": 200, "body": b"SQL error"})
        assert m3["matched"] is False and m3["status_match"] is False

    def test_replay_remediated(self):
        """原始触发包重放：响应不再匹配（修复）→ matched_original=0 → remediated。"""
        client = FakeClient()
        http = FakeHttp([FakeResp(403, b"forbidden")])
        eng = ReplayEngine(client=client, http_client=http)
        full = {"request": "POST /login HTTP/1.1\r\nHost: x\r\n\r\nuser=admin&pass=admin'",
                "response": "HTTP/1.1 200 OK\r\n\r\nSQL error", "url": "http://x/login",
                "method": "POST", "status": 200}
        result = eng.replay_http(full)
        assert result["matched_original"] == 0
        assert result["result"] == "remediated"

    def test_replay_unchanged(self):
        """仍触发（响应匹配）→ matched_original > 0 → unchanged。"""
        client = FakeClient()
        http = FakeHttp([FakeResp(200, b"SQL error")])
        eng = ReplayEngine(client=client, http_client=http)
        full = {"request": "POST /login HTTP/1.1\r\nHost: x\r\n\r\nuser=admin",
                "response": "HTTP/1.1 200 OK\r\n\r\nSQL error", "url": "http://x/login",
                "method": "POST", "status": 200}
        result = eng.replay_http(full)
        assert result["matched_original"] >= 1
        assert result["result"] == "unchanged"

    def test_replay_run_remediated_records_retest_confirmation(self):
        """run 主入口：remediated → record_retest_confirmation(kind='replay')。"""
        client = FakeClient()
        http = FakeHttp([FakeResp(403, b"forbidden")])
        eng = ReplayEngine(client=client, http_client=http)
        result = eng.run("e-001", "fd-001", trigger_traffic_id="tr-001", payload_variants=0, is_http=True)
        assert result["result"] == "remediated"
        assert any(c["fid"] == "fd-001" and c["kind"] == "replay" for c in client.retest_confirmations)

    def test_replay_ledger_idempotent(self):
        """账本幂等（TV-44）：同 fid 同 kind 重复触发不 +1。"""
        client = FakeClient()
        http = FakeHttp([FakeResp(403, b"forbidden")])
        eng = ReplayEngine(client=client, http_client=http)
        eng.run("e-001", "fd-001", trigger_traffic_id="tr-001", payload_variants=0, is_http=True)
        eng.run("e-001", "fd-001", trigger_traffic_id="tr-001", payload_variants=0, is_http=True)
        replays = [c for c in client.retest_confirmations if c["fid"] == "fd-001" and c["kind"] == "replay"]
        assert len(replays) == 1  # 幂等：不重复 +1

    def test_replay_command_remediated(self):
        """命令确定性重放（capture §6.1）：原成功回显不复现（现失败）→ remediated。"""
        eng = ReplayEngine(client=None)
        cmd = {"command": "exit 1", "exit_code": 0, "stdout": "Last login: ..."}
        result = eng.replay_command(cmd)
        assert result["result"] == "remediated"


# ---------------------------------------------------------------------------
# F9 进度流分类（验收点 6：scanner 输出含 "error" 不产生 error 事件）
# ---------------------------------------------------------------------------


class TestProgressStream:
    def test_scanner_error_not_error_event(self):
        """stdout 含 "error"/"failed" 字样不算 error（F9 防噪声）。"""
        kind, level = classify_line("nuclei: found 2 errors, 1 failed result")
        assert kind == "output"
        assert level != "error"

    def test_stderr_is_error(self):
        kind, level = classify_line("something failed", stream="stderr")
        assert kind == "error"

    def test_traceback_is_error(self):
        kind, level = classify_line("Traceback (most recent call last):\n  File x")
        assert kind == "error"

    def test_command_prefix(self):
        kind, _level = classify_line("$ curl http://x")
        assert kind == "command"

    def test_injected_status_prefix(self):
        kind, _level = classify_line("⚑ prepare session")
        assert kind == "status"

    def test_tool_call_line(self):
        kind, _level = classify_line('<tool>Read("file")</tool>')
        assert kind == "tool"

    def test_structured_json_maps_type(self):
        kind, level = classify_line('{"type": "step", "message": "running scan"}')
        assert kind == "step"
        kind2, _lvl = classify_line('{"type": "command", "message": "curl"}')
        assert kind2 == "command"

    def test_summary_truncated_to_512(self):
        msg = "x" * 1000
        s = summarize_event(msg, max_bytes=512)
        assert len(s.encode("utf-8")) <= 512


# ---------------------------------------------------------------------------
# 写回器（findings/coverage）
# ---------------------------------------------------------------------------


class TestWriters:
    def test_findings_writer_dedup_appends_evidence(self):
        """B3：FINDING_DUP 命中已有 → 不重复建单，追加证据。"""
        client = FakeClient()

        def _raise_dup(eid, payload, *, detected_by=None, actor="agent"):
            raise CairnClientError(FINDING_DUP, "duplicate", http_status=409, detail={"finding_id": "fd-001"})

        client.create_finding = _raise_dup  # monkeypatch：命中已有
        fw = FindingsWriter(client, retries=0)
        created = fw.write("e-001", findings=[{"title": "SQLi", "severity": "high", "asset": "http://x",
                                               "traffic_ids": ["tr-001"]}], detected_by="worker-a")
        assert created[0]["duplicate"] is True
        # 追加证据到已有 fd-001
        assert any(c[1].endswith("/findings/fd-001/traffic") for c in client.calls)

    def test_coverage_writer_write_result_idempotency_key(self):
        client = FakeClient()
        cw = CoverageWriter(client)
        cw.write_result("e-001", item_ids=["c-013"], depth_achieved="standard", outcome="finding_created",
                        intent_id="i-001", fact_id="f001")
        assert client.write_coverage_results[0]["idempotency_key"] == "c-013:i-001"

    def test_coverage_writer_claim_release(self):
        client = FakeClient()
        cw = CoverageWriter(client)
        assert cw.claim_item("e-001", "c-013", "i-001") is True
        client.claim_results["c-013"] = False
        assert cw.claim_item("e-001", "c-013", "i-002") is False
        cw.release_item("e-001", "c-013", "i-002")
        assert any(c[1].endswith("/release") for c in client.calls)


# ---------------------------------------------------------------------------
# 31 mock 驱动就绪后的任务逻辑复验（importorskip 保护；50 全量回归再复验）
# ---------------------------------------------------------------------------


class TestMockFullChain:
    """用 31 的 MockDriver（真实子进程脚本）+ 11 的 LocalBackend 跑任务逻辑。

    31 未就绪时这些用例被 ``pytest.importorskip`` 跳过（46 skipped 中 1 个）。
    """

    def _mock_driver(self, phase: str, cfg: dict):
        from cairn.dispatcher.workers.adapters.mock import MockDriver

        return MockDriver(execution="local", worker_env={f"MOCK_{phase.upper()}": json.dumps(cfg)})

    def test_mock_driver_run_reason(self):
        pytest.importorskip("cairn.dispatcher.workers.adapters.mock")
        from cairn.dispatcher.runtime.local_backend import LocalBackend

        ctx = make_ctx(FakeClient())
        driver = self._mock_driver("reason", {
            "delay": [0.0, 0.0],
            "outcomes": {"intents": "1.0", "finalize": "0.0"},
            "payload": {"intents": [{"from": ["f001"], "description": "mock sqli",
                                     "coverage_item_ids": ["c-001"]}]},
        })
        gaps = [{"item_id": "c-001", "target_id": "t-001", "target_value": "10.0.0.5",
                 "test_type_id": "tt_web_sqli", "test_type_name": "SQLi", "depth": "deep", "priority": 0.9}]
        result = run_reason(ctx, driver=driver, backend=LocalBackend(), gaps=gaps, graph_yaml="id: f001")
        assert result.status == "success"
        assert result.data["intents"][0]["coverage_item_ids"] == ["c-001"]

    def test_mock_driver_run_verify(self):
        pytest.importorskip("cairn.dispatcher.workers.adapters.mock")
        from cairn.dispatcher.runtime.local_backend import LocalBackend

        ctx = make_ctx(FakeClient(), worker="worker-B")
        driver = self._mock_driver("verify", {
            "delay": [0.0, 0.0],
            "outcomes": {"confirmed": "1.0", "rejected": "0.0", "needs_more_evidence": "0.0"},
            "payload": {"verified_severity": "high", "verified_traffic_ids": ["tr-001"],
                        "reason": "mock confirm"},
        })
        finding = {"id": "fd-001", "title": "SQLi", "severity": "high",
                   "traffic_links": [{"traffic_id": "tr-001", "role": "trigger"}],
                   "detected_by": "worker-A"}
        result = run_verify(ctx, driver=driver, backend=LocalBackend(), finding=finding, eid="e-001")
        assert result.status == "success"
        assert result.data["verdict"] == "confirmed"
        assert result.data["independence"] == "cross_worker"
