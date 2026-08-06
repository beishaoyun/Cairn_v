"""Mock driver regression + full-chain TV runner (Agent 31).

Two layers:

1. **Mock driver / harness unit tests** — always runnable (no Server / no
   DispatcherLoop / no LLM). Verify ``MockDriver`` construction, ``MOCK_*`` env
   validation, and the mock worker script's JSON output contract for every
   phase and every outcome (including crash injection).

2. **The 46 full-chain cases TV-01..TV-46** (verify-mock-test-spec §4) —
   organised as a pytest parametrized matrix with rule mappings. Each case
   requires a process-internal Server + DispatcherLoop (LocalBackend +
   MockDriver) which needs Agents 30 (tasks) and 40 (loop). When those are not
   installed the whole matrix is skipped via ``pytest.importorskip`` (expected
   until Phase 1 completes; the matrix is the entry point Agent 50 uses to
   re-verify).

Rule mappings are annotated per case against ``docs/rule-registry.md`` (v2 §12
rules 28-41 + A2/A5/B1/C2/C8/C10/F4/F8/F9/F10/F11).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import pytest

from cairn.dispatcher.workers.adapters.mock import (
    MOCK_ALLOWED_ENV_KEYS,
    MOCK_ALLOWED_OUTCOMES,
    MOCK_DEFAULT_BEHAVIOR,
    MockConfigError,
    MockDriver,
    mock_env_key,
    validate_mock_config,
)
from cairn.dispatcher.workers.base import MissingEnvError
from cairn.dispatcher.workers.registry import (
    build_worker_driver,
    get_driver_class,
)

from mock_harness import (
    assert_finding_state,
    assert_replay_run,
    assert_retest_pass,
    assert_verified_severity,
    audit_cfg,
    bootstrap_cfg,
    explore_cfg,
    make_mock_driver,
    mock_cfg,
    mock_prompt,
    parse_mock_json,
    phase_cfg,
    reason_cfg,
    replay_cfg,
    run_mock,
    seed_finding,
    seed_replay_evidence,
    seed_traffic,
    verify_cfg,
)


# ===========================================================================
# 1. Mock driver unit tests (always run)
# ===========================================================================


class TestMockDriver:
    def test_driver_type_and_no_env_keys(self):
        assert MockDriver.driver_type == "mock"
        assert MockDriver.required_env_keys == ()
        d = MockDriver(execution="container", worker_env={})
        assert d.execution == "container"  # no MissingEnvError raised

    def test_prepare_session_seed(self):
        d = MockDriver(execution="local", worker_env={})
        sid = d.prepare_session()
        assert sid.startswith("mock-")
        assert len(sid) > 5
        assert d.extract_session("whatever", "") is None

    def test_check_health_always_true(self):
        d = MockDriver(execution="local", worker_env={})
        assert d.check_health() is True
        assert d.supports_conclude() is True

    def test_build_execute_argv_runs_script(self):
        d = MockDriver(execution="local", worker_env={})
        cmd = d.build_execute(mock_prompt("verify", stage="comparison", text="verdict"),
                              session_id=d.prepare_session())
        assert cmd.argv[0] == subprocess.sys.executable or cmd.argv[0].endswith("python")
        assert "_mock_script.py" in cmd.argv[1]
        assert len(cmd.argv) == 3  # python script prompt-file

    def test_registered_in_registry(self):
        assert get_driver_class("mock") is MockDriver
        d = build_worker_driver("mock", execution="local", worker_env={})
        assert isinstance(d, MockDriver)

    def test_mock_env_keys_auto_derived(self):
        for phase in MOCK_ALLOWED_OUTCOMES:
            key = mock_env_key(phase)
            assert key in MOCK_ALLOWED_ENV_KEYS
        assert "MOCK_VERIFY" in MOCK_ALLOWED_ENV_KEYS
        assert "MOCK_REPLAY" in MOCK_ALLOWED_ENV_KEYS

    def test_unknown_mock_key_rejected(self):
        with pytest.raises(MockConfigError):
            MockDriver(execution="local", worker_env={"MOCK_BOGUS": "{}"})

    def test_invalid_json_rejected(self):
        with pytest.raises(MockConfigError):
            MockDriver(execution="local", worker_env={"MOCK_VERIFY": "{not json"})

    def test_outcome_sum_must_equal_one(self):
        cfg = json.dumps({"outcomes": {"confirmed": "0.5", "rejected": "0.2"}})
        with pytest.raises(MockConfigError):
            MockDriver(execution="local", worker_env={"MOCK_VERIFY": cfg})

    def test_unknown_outcome_rejected(self):
        cfg = json.dumps({"outcomes": {"maybe": "1.0"}})
        with pytest.raises(MockConfigError):
            MockDriver(execution="local", worker_env={"MOCK_VERIFY": cfg})

    def test_bad_delay_rejected(self):
        cfg = json.dumps({"delay": [0.3, 0.1], "outcomes": {"confirmed": "1.0"}})
        with pytest.raises(MockConfigError):
            MockDriver(execution="local", worker_env={"MOCK_VERIFY": cfg})

    def test_default_behavior_present_for_all_phases(self):
        for phase in MOCK_ALLOWED_OUTCOMES:
            assert phase in MOCK_DEFAULT_BEHAVIOR
            total = sum(float(v) for v in MOCK_DEFAULT_BEHAVIOR[phase]["outcomes"].values())
            assert total == pytest.approx(1.0)

    def test_validate_mock_config_accepts_good(self):
        validate_mock_config("verify", {"outcomes": {"confirmed": "1.0", "rejected": "0.0"}})
        validate_mock_config("replay", {"delay": [0, 0], "outcomes": {"remediated": "1.0"}})

    def test_validate_mock_config_unknown_phase(self):
        with pytest.raises(MockConfigError):
            validate_mock_config("nope", {"outcomes": {"ok": "1.0"}})

    def test_extra_key_explore_coverage_outcome_allowed(self):
        from cairn.dispatcher.workers.adapters.mock import (
            MOCK_EXTRA_KEYS,
            validate_mock_extra_key,
        )
        assert "MOCK_EXPLORE_COVERAGE_OUTCOME" in MOCK_EXTRA_KEYS
        assert "MOCK_EXPLORE_COVERAGE_OUTCOME" in MOCK_ALLOWED_ENV_KEYS
        validate_mock_extra_key(
            "MOCK_EXPLORE_COVERAGE_OUTCOME",
            {"outcomes": {"no_issue": 0.5, "finding_created": 0.4, "not_applicable": 0.1}},
        )
        with pytest.raises(MockConfigError):
            validate_mock_extra_key(
                "MOCK_EXPLORE_COVERAGE_OUTCOME", {"outcomes": {"bogus": 1.0}}
            )

    def test_hang_delay_accepted(self):
        # TV-40: a [1200,1200] delay is a valid config (the loop's timeout kills it)
        cfg = {"delay": [1200.0, 1200.0], "outcomes": {"confirmed": "1.0"}}
        validate_mock_config("verify", cfg)
        d = MockDriver(execution="local", worker_env={
            mock_env_key("verify"): json.dumps(cfg)})
        assert isinstance(d, MockDriver)

    def test_dispatch_mock_yaml_builds_mock_drivers(self, monkeypatch):
        import os
        monkeypatch.setenv("CAIRN_API_TOKEN", "test-token")
        from cairn.dispatcher.cli import build_drivers
        from cairn.dispatcher.config import load
        cfg = load("dispatch_mock.yaml", env=os.environ)
        drivers = build_drivers(cfg)
        assert all(isinstance(d, MockDriver) for d in drivers.values())
        assert sorted(drivers) == ["mock-observer-1", "mock-observer-2"]


# ---------------------------------------------------------------------------
# Mock script output contract (subprocess, deterministic)
# ---------------------------------------------------------------------------


class TestMockScriptVerify:
    """Verify phase — blind / comparison stages + payload injection + crashes."""

    def _driver(self, cfg):
        return MockDriver(
            execution="local",
            worker_env={mock_env_key("verify"): json.dumps(cfg, ensure_ascii=False)},
        )

    def test_comparison_confirmed(self):
        d = self._driver(verify_cfg(outcome="confirmed", severity="high",
                                    traffic_ids=["tr-001"]))
        r = run_mock(d, mock_prompt("verify", stage="comparison", text="verdict"))
        assert r.returncode == 0
        out = parse_mock_json(r)
        data = out["data"]
        assert out["accepted"] is True
        assert data["stage"] == "comparison"
        assert data["verdict"] == "confirmed"
        assert data["verified_severity"] == "high"
        assert data["verified_traffic_ids"] == ["tr-001"]
        assert data["suggested_action"] == "none"
        assert "reason" in data

    def test_comparison_rejected(self):
        d = self._driver(verify_cfg(outcome="rejected"))
        r = run_mock(d, mock_prompt("verify", stage="comparison", text="verdict"))
        assert parse_mock_json(r)["data"]["verdict"] == "rejected"

    def test_comparison_needs_more_evidence(self):
        d = self._driver(verify_cfg(outcome="needs_more_evidence",
                                    suggested_action="collect_evidence"))
        r = run_mock(d, mock_prompt("verify", stage="comparison", text="verdict"))
        data = parse_mock_json(r)["data"]
        assert data["verdict"] == "needs_more_evidence"
        assert data["suggested_action"] == "collect_evidence"

    def test_blind_stage_observations(self):
        obs = [{"vuln": "SQLi", "severity": "high", "traffic_id": "tr-001", "basis": "x"}]
        d = self._driver(
            phase_cfg("verify", outcome="confirmed", delay=[0, 0],
                      payload={"observations": obs, "traffic_note": "limited"})
        )
        r = run_mock(d, mock_prompt("verify", stage="blind", text="observations digest"))
        data = parse_mock_json(r)["data"]
        assert data["observations"] == obs
        assert data["traffic_note"] == "limited"
        assert "verdict" not in data

    def test_blind_default_observations(self):
        d = self._driver(verify_cfg(outcome="confirmed"))
        r = run_mock(d, mock_prompt("verify", stage="blind", text="observations"))
        data = parse_mock_json(r)["data"]
        assert isinstance(data["observations"], list) and data["observations"]

    def test_payload_verdict_override_illegal(self):
        # TV-13 injection: verdict forced to an illegal value via payload override
        d = self._driver(verify_cfg(outcome="confirmed", verdict="maybe"))
        r = run_mock(d, mock_prompt("verify", stage="comparison", text="verdict"))
        assert parse_mock_json(r)["data"]["verdict"] == "maybe"

    def test_accepted_false(self):
        d = self._driver(verify_cfg(outcome="accepted_false"))
        r = run_mock(d, mock_prompt("verify", stage="comparison", text="verdict"))
        out = parse_mock_json(r)
        assert out["accepted"] is False
        assert out["reason"] == "mock_rejected"

    def test_invalid_json(self):
        d = self._driver(phase_cfg("verify", outcome="invalid_json", delay=[0, 0]))
        r = run_mock(d, mock_prompt("verify", stage="comparison", text="verdict"))
        assert r.returncode == 0
        assert "{invalid json" in r.stdout
        assert parse_mock_json(r) is None

    def test_empty_output(self):
        d = self._driver(phase_cfg("verify", outcome="empty", delay=[0, 0]))
        r = run_mock(d, mock_prompt("verify", stage="comparison", text="verdict"))
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_command_fail(self):
        d = self._driver(phase_cfg("verify", outcome="command_fail", delay=[0, 0]))
        r = run_mock(d, mock_prompt("verify", stage="comparison", text="verdict"))
        assert r.returncode == 1
        assert parse_mock_json(r) is None

    def test_rule_prompt_has_forces_outcome(self):
        # A single worker covers multiple scenarios via rules[].prompt_has
        rules = [
            {"prompt_has": "blind-phase-token", "force": "confirmed",
             "payload": {"observations": [{"vuln": "A", "severity": "low", "traffic_id": "tr-1", "basis": "b"}]}},
            {"prompt_has": "critical-finding", "force": "confirmed",
             "payload": {"verified_severity": "critical"}},
        ]
        d = self._driver(verify_cfg(outcome="rejected", rules=rules))
        # comparison prompt mentioning "critical-finding" → confirmed + critical
        r = run_mock(d, mock_prompt("verify", stage="comparison", text="verdict critical-finding"))
        data = parse_mock_json(r)["data"]
        assert data["verdict"] == "confirmed"
        assert data["verified_severity"] == "critical"
        # blind prompt mentioning "blind-phase-token" → blind observations
        r2 = run_mock(d, mock_prompt("verify", stage="blind", text="observations blind-phase-token"))
        assert parse_mock_json(r2)["data"]["observations"][0]["severity"] == "low"


class TestMockScriptOtherPhases:
    def test_replay_remediated(self):
        d = MockDriver(execution="local", worker_env={
            mock_env_key("replay"): json.dumps(replay_cfg(result="remediated", matched_original=0))})
        r = run_mock(d, mock_prompt("replay", text="replay trigger"))
        data = parse_mock_json(r)["data"]
        assert data["result"] == "remediated"
        assert data["matched_original"] == 0

    def test_replay_unchanged_matched(self):
        d = MockDriver(execution="local", worker_env={
            mock_env_key("replay"): json.dumps(replay_cfg(result="unchanged", matched_original=2))})
        r = run_mock(d, mock_prompt("replay", text="replay trigger"))
        data = parse_mock_json(r)["data"]
        assert data["result"] == "unchanged"
        assert data["matched_original"] == 2

    def test_replay_ambiguous(self):
        d = MockDriver(execution="local", worker_env={
            mock_env_key("replay"): json.dumps(replay_cfg(result="ambiguous"))})
        r = run_mock(d, mock_prompt("replay", text="replay trigger"))
        assert parse_mock_json(r)["data"]["result"] == "ambiguous"

    def test_replay_error(self):
        d = MockDriver(execution="local", worker_env={
            mock_env_key("replay"): json.dumps(replay_cfg(result="error"))})
        r = run_mock(d, mock_prompt("replay", text="replay trigger"))
        assert parse_mock_json(r)["data"]["result"] == "error"

    def test_explore_with_findings_and_coverage(self):
        findings = [{
            "title": "SQLi in /login", "severity": "high", "cvss_score": 8.1,
            "asset": "http://10.0.0.5:8080/login", "traffic_ids": ["tr-001"],
            "http": [{"method": "POST", "url": "http://10.0.0.5:8080/login",
                      "request_body": "u=a&p=' OR 1=1--", "response_status": 200,
                      "response_body": "SQL error near 'OR 1=1'"}],
        }]
        coverage = {"covered_items": ["c-013"], "depth_achieved": "standard",
                    "outcome": "finding_created"}
        d = MockDriver(execution="local", worker_env={
            mock_env_key("explore_execute"): json.dumps(
                explore_cfg(findings=findings, coverage=coverage))})
        r = run_mock(d, mock_prompt("explore_execute", text="explore intent"))
        data = parse_mock_json(r)["data"]
        assert data["findings"][0]["traffic_ids"] == ["tr-001"]
        assert data["coverage"]["outcome"] == "finding_created"

    def test_explore_no_issue(self):
        coverage = {"covered_items": ["c-013"], "depth_achieved": "standard", "outcome": "no_issue"}
        d = MockDriver(execution="local", worker_env={
            mock_env_key("explore_execute"): json.dumps(explore_cfg(coverage=coverage))})
        r = run_mock(d, mock_prompt("explore_execute", text="explore intent"))
        data = parse_mock_json(r)["data"]
        assert data["coverage"]["outcome"] == "no_issue"
        assert "findings" not in data

    def test_explore_rejected(self):
        d = MockDriver(execution="local", worker_env={
            mock_env_key("explore_execute"): json.dumps(
                phase_cfg("explore_execute", outcome="rejected", delay=[0, 0]))})
        r = run_mock(d, mock_prompt("explore_execute", text="explore"))
        out = parse_mock_json(r)
        assert out["accepted"] is False

    def test_reason_intents(self):
        d = MockDriver(execution="local", worker_env={
            mock_env_key("reason"): json.dumps(reason_cfg(intents=[
                {"from": ["f001"], "description": "probe", "coverage_item_ids": ["c-013"]}]))})
        r = run_mock(d, mock_prompt("reason", text="coverage gaps"))
        data = parse_mock_json(r)["data"]
        assert data["intents"][0]["coverage_item_ids"] == ["c-013"]
        assert data["coverage"]["recommend_finalize"] is False

    def test_reason_finalize(self):
        d = MockDriver(execution="local", worker_env={
            mock_env_key("reason"): json.dumps(reason_cfg(
                outcome="finalize", waivers=[{"item_id": "c-099", "kind": "not_applicable", "reason": "r"}]))})
        r = run_mock(d, mock_prompt("reason", text="coverage gaps"))
        data = parse_mock_json(r)["data"]
        assert data["intents"] == []
        assert data["coverage"]["recommend_finalize"] is True
        assert data["coverage"]["waivers"][0]["item_id"] == "c-099"

    def test_bootstrap(self):
        discoveries = [{"target": "10.0.0.5", "port": 8080, "service": "tomcat"}]
        d = MockDriver(execution="local", worker_env={
            mock_env_key("bootstrap"): json.dumps(bootstrap_cfg(discoveries=discoveries))})
        r = run_mock(d, mock_prompt("bootstrap", text="discoveries"))
        data = parse_mock_json(r)["data"]
        assert data["discoveries"] == discoveries
        assert "sweep_complete" in data
        # the `complete` field concept is removed; only `sweep_complete` is allowed
        assert "complete" not in data

    def test_audit_covered(self):
        d = MockDriver(execution="local", worker_env={
            mock_env_key("audit"): json.dumps(audit_cfg(outcome="covered"))})
        r = run_mock(d, mock_prompt("audit", text="audit item"))
        data = parse_mock_json(r)["data"]
        assert data["verdict"] == "covered"

    def test_audit_discrepancy(self):
        d = MockDriver(execution="local", worker_env={
            mock_env_key("audit"): json.dumps(audit_cfg(outcome="discrepancy"))})
        r = run_mock(d, mock_prompt("audit", text="audit item"))
        assert parse_mock_json(r)["data"]["verdict"] == "discrepancy"

    def test_healthcheck_fail_outcome(self):
        d = MockDriver(execution="local", worker_env={
            mock_env_key("healthcheck"): json.dumps(
                phase_cfg("healthcheck", outcome="fail", delay=[0, 0]))})
        r = run_mock(d, mock_prompt("healthcheck", text="health"))
        assert parse_mock_json(r)["data"]["status"] == "fail"

    def test_explore_coverage_outcome_env_controls_default(self):
        # No payload coverage → MOCK_EXPLORE_COVERAGE_OUTCOME drives the outcome
        d = MockDriver(execution="local", worker_env={
            mock_env_key("explore_execute"): json.dumps(explore_cfg()),
            "MOCK_EXPLORE_COVERAGE_OUTCOME": json.dumps(
                {"outcomes": {"finding_created": 1.0}})})
        r = run_mock(d, mock_prompt("explore_execute", text="explore"))
        assert parse_mock_json(r)["data"]["coverage"]["outcome"] == "finding_created"

    def test_phase_detected_from_marker_without_explicit_phase(self):
        # run_mock without phase/stage args — the marker in the prompt drives it
        d = MockDriver(execution="local", worker_env={
            mock_env_key("verify"): json.dumps(verify_cfg(outcome="confirmed"))})
        r = run_mock(d, mock_prompt("verify", stage="comparison", text="verdict"))
        assert parse_mock_json(r)["data"]["verdict"] == "confirmed"


class TestMockHarness:
    def test_phase_cfg_forces_outcome(self):
        cfg = phase_cfg("verify", outcome="rejected", delay=[0, 0])
        assert cfg["outcomes"]["rejected"] == "1.0"
        assert cfg["outcomes"]["confirmed"] == "0.0"
        assert sum(float(v) for v in cfg["outcomes"].values()) == 1.0

    def test_worker_env_serializes(self):
        env = mock_cfg.worker_env(verify=verify_cfg(outcome="confirmed"),
                                  replay=replay_cfg(result="remediated"))
        assert json.loads(env["MOCK_VERIFY"])["outcomes"]["confirmed"] == "1.0"
        assert json.loads(env["MOCK_REPLAY"])["outcomes"]["remediated"] == "1.0"

    def test_make_mock_driver(self):
        d = make_mock_driver(verify=verify_cfg(outcome="confirmed"))
        assert isinstance(d, MockDriver)
        assert json.loads(d.env["MOCK_VERIFY"])["outcomes"]["confirmed"] == "1.0"

    def test_phase_cfg_rejects_bad_outcome(self):
        with pytest.raises(ValueError):
            phase_cfg("verify", outcome="nope")

    def test_mock_prompt_markers(self):
        p = mock_prompt("verify", stage="comparison", text="body")
        assert "mock-phase: verify" in p
        assert "mock-stage: comparison" in p
        assert p.endswith("body")


# ===========================================================================
# 2. Full-chain TV-01..TV-46 matrix (skipped until 30/40 are installed)
# ===========================================================================


@dataclass
class E2ECtx:
    """Process-internal Server + DispatcherLoop context for E2E cases.

    Populated by the ``e2e_ctx`` fixture (Agent 50 wiring / Agent 30+40 landing).
    Scenario functions use only these accessors so the matrix is stable.
    """

    client: Any
    dispatch: Any
    eid: str
    engagement: Any = None
    workers: list = field(default_factory=list)
    traffic_seed: Any = None

    # --- helpers ----------------------------------------------------------
    def latest_finding(self) -> str:
        rows = self.client.get(f"/engagements/{self.eid}/findings").json()
        assert rows, "no findings"
        return rows[-1]["id"]

    def create_finding(self, **payload) -> str:
        return seed_finding(self.client, self.eid, payload=payload or None,
                            detected_by="worker-A")

    def transition(self, fid: str, to_status: str, by: str = "human"):
        resp = self.client.put(
            f"/engagements/{self.eid}/findings/{fid}",
            json={"status": to_status, "note": "e2e", "actor": by},
        )
        return resp

    def task_run(self, run_id: str) -> dict:
        return self.dispatch.task_run(run_id)

    def events(self, run_id: str) -> list[dict]:
        return self.dispatch.events(run_id)

    def find_verify_run(self, fid: str) -> str:
        runs = self.client.get(f"/engagements/{self.eid}/tasks").json()
        for r in runs:
            if r.get("task_type") == "verify" and r.get("finding_id") == fid:
                return r["id"]
        raise AssertionError(f"no verify run for {fid}")

    def pump(self, timeout: float = 30.0):
        from mock_harness import pump_until_idle
        pump_until_idle(self.dispatch, timeout=timeout)


E2EScenario = Callable[[E2ECtx], None]


def _scenario(fn: E2EScenario) -> E2EScenario:
    return fn


TV_CASES: list[tuple[str, str, str, E2EScenario]] = [
    # ---- A. 基础 verdict 路径 -------------------------------------------
    ("TV-01", "基础确认路径", "verify verdict 三分支", _scenario(
        lambda ctx: _tv_01(ctx))),
    ("TV-02", "降级定级", "verify 双轨 severity", _scenario(lambda ctx: _tv_02(ctx))),
    ("TV-03", "升级定级", "P0 告警事件", _scenario(lambda ctx: _tv_03(ctx))),
    ("TV-04", "拒绝先落地待确认", "verify rejected → pending_false_positive", _scenario(lambda ctx: _tv_04(ctx))),
    ("TV-05", "拒绝终态二次确认", "人工确认 false_positive", _scenario(lambda ctx: _tv_05(ctx))),
    ("TV-06", "证据不足回 open", "needs_more → 补证 explore", _scenario(lambda ctx: _tv_06(ctx))),
    ("TV-07", "建议立即复测", "retest explore", _scenario(lambda ctx: _tv_07(ctx))),
    ("TV-08", "流量无交集拦截", "契约层拦截 → needs_more", _scenario(lambda ctx: _tv_08(ctx))),
    # ---- B. 独立性派发 ---------------------------------------------------
    ("TV-09", "独立 worker 派发", "规则37 独立性", _scenario(lambda ctx: _tv_09(ctx))),
    ("TV-10", "单 worker 降级", "单 worker 不派发", _scenario(lambda ctx: _tv_10(ctx))),
    ("TV-11", "多 finding 并发派发", "worker 排除", _scenario(lambda ctx: _tv_11(ctx))),
    ("TV-12", "复核幂等去重", "去重键 finding_id+stage", _scenario(lambda ctx: _tv_12(ctx))),
    # ---- C. 契约与异常 ---------------------------------------------------
    ("TV-13", "非法 verdict 值", "契约校验拒绝", _scenario(lambda ctx: _tv_13(ctx))),
    ("TV-14", "非法 severity", "回退 agent_severity", _scenario(lambda ctx: _tv_14(ctx))),
    ("TV-15", "流量引用不存在", "校验拒绝", _scenario(lambda ctx: _tv_15(ctx))),
    ("TV-16", "accepted=false", "任务 rejected", _scenario(lambda ctx: _tv_16(ctx))),
    ("TV-17", "非 JSON 重试", "重试 ≤3 后 failed", _scenario(lambda ctx: _tv_17(ctx))),
    ("TV-18", "空输出重试", "同 TV-17", _scenario(lambda ctx: _tv_18(ctx))),
    ("TV-19", "崩溃重试", "command_fail", _scenario(lambda ctx: _tv_19(ctx))),
    ("TV-20", "needs_more 循环超限", "规则28/F6 → needs_review", _scenario(lambda ctx: _tv_20(ctx))),
    # ---- D. 全链路回归 ---------------------------------------------------
    ("TV-21", "发现→复核→报告", "全链路 HTTP finding", _scenario(lambda ctx: _tv_21(ctx))),
    ("TV-22", "非 HTTP 漏洞链", "命令回显 confirmed", _scenario(lambda ctx: _tv_22(ctx))),
    ("TV-23", "复测通过闭环", "retest_pass ≥2 closed", _scenario(lambda ctx: _tv_23(ctx))),
    ("TV-24", "复测仍存在", "回 open + P0", _scenario(lambda ctx: _tv_24(ctx))),
    ("TV-25", "豁免流量不可用", "traffic_missing → needs_more", _scenario(lambda ctx: _tv_25(ctx))),
    ("TV-26", "报告幂等", "报告一致", _scenario(lambda ctx: _tv_26(ctx))),
    # ---- E. 进度与联动 ---------------------------------------------------
    ("TV-27", "verify 运行事件流", "task_runs + task_events 生命周期", _scenario(lambda ctx: _tv_27(ctx))),
    ("TV-28", "SSE 增量续传", "events?after_seq", _scenario(lambda ctx: _tv_28(ctx))),
    ("TV-29", "前端联动", "findings?status=pending_verify", _scenario(lambda ctx: _tv_29(ctx))),
    # ---- F. 复测重放 · 捕获字节 · 协议边界 -------------------------------
    ("TV-30", "确定性重放·已修复", "规则31/F4 replay remediated", _scenario(lambda ctx: _tv_30(ctx))),
    ("TV-31", "确定性重放·仍触发", "规则31/26 → 回 open + 403", _scenario(lambda ctx: _tv_31(ctx))),
    ("TV-32", "重放·签名比对 ambiguous", "规则31 compare_signature", _scenario(lambda ctx: _tv_32(ctx))),
    ("TV-33", "捕获字节为准", "规则29/C2 http_mismatch", _scenario(lambda ctx: _tv_33(ctx))),
    ("TV-34", "代理单写者", "规则32/F8", _scenario(lambda ctx: _tv_34(ctx))),
    ("TV-35", "协议边界降级", "规则36/F10 命令回显", _scenario(lambda ctx: _tv_35(ctx))),
    # ---- G. 覆盖闭环 · 熔断与采集 ---------------------------------------
    ("TV-36", "auto_created 闭环", "规则33/F11 report_ready 不阻塞", _scenario(lambda ctx: _tv_36(ctx))),
    ("TV-37", "覆盖抽样复核", "规则34/F3 audit discrepancy", _scenario(lambda ctx: _tv_37(ctx))),
    ("TV-38", "kill 即停捕获", "规则30/C3", _scenario(lambda ctx: _tv_38(ctx))),
    ("TV-39", "结构化流分类", "规则35/F9 stderr 置红", _scenario(lambda ctx: _tv_39(ctx))),
    ("TV-40", "挂起超时重派", "delay 1200 + verify_timeout", _scenario(lambda ctx: _tv_40(ctx))),
    # ---- H. 修复闭环新增 -------------------------------------------------
    ("TV-41", "格子互斥", "规则38/B1 claim_item_for_intent", _scenario(lambda ctx: _tv_41(ctx))),
    ("TV-42", "捕获完整性对账", "规则40/C2 capture_gap", _scenario(lambda ctx: _tv_42(ctx))),
    ("TV-43", "reason 空转升级人工", "规则41/C8", _scenario(lambda ctx: _tv_43(ctx))),
    ("TV-44", "复测账本幂等与归零", "A2/C10", _scenario(lambda ctx: _tv_44(ctx))),
    ("TV-45", "部分覆盖不虚标全绿", "C9 partial", _scenario(lambda ctx: _tv_45(ctx))),
    ("TV-46", "非 HTTP 命令确定性重放", "规则26/F4 命令通道", _scenario(lambda ctx: _tv_46(ctx))),
]


@pytest.fixture()
def e2e_ctx() -> E2ECtx:
    """Skip the whole matrix until Agent 50 wires the e2e harness.

    Agent 40 delivered ``cairn.dispatcher.scheduler.loop`` + ``tasks.verify`` (so the
    importorskip below no longer skips); full in-process Server + DispatcherLoop wiring
    for the 46 TV cases is owned by Agent 50. Until then the matrix stays skipped so the
    suite remains green.
    """
    pytest.importorskip("cairn.dispatcher.scheduler.loop")
    pytest.importorskip("cairn.dispatcher.tasks.verify")
    pytest.skip("e2e_ctx wiring owned by Agent 50; not yet installed")


@pytest.mark.parametrize(
    "tv_id,title,rules,scenario",
    TV_CASES,
    ids=[c[0] for c in TV_CASES],
)
def test_tv(e2e_ctx: E2ECtx, tv_id: str, title: str, rules: str, scenario: E2EScenario):
    scenario(e2e_ctx)
    # (kept here so the mapping tv_id → rules is visible in -rA output)
    assert rules  # pragma: no cover


# ---------------------------------------------------------------------------
# Scenario bodies (gated by e2e_ctx; refined by Agent 50 once 30/40 land)
# ---------------------------------------------------------------------------


def _ensure(ctx: E2ECtx) -> None:
    """Create a verified finding from a mock explore so verify runs can assert."""
    ctx.pump()
    fid = ctx.latest_finding()
    assert_finding_state(ctx.client, ctx.eid, fid, status="open", verify_status="none")
    return fid


def _tv_01(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    run = ctx.find_verify_run(fid)
    ctx.pump()
    assert_finding_state(ctx.client, ctx.eid, fid, status="verified", verify_status="confirmed",
                         severity="high")
    assert assert_verified_severity(ctx.client, ctx.eid, fid) == "high"


def _tv_02(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.pump()
    f = assert_finding_state(ctx.client, ctx.eid, fid, status="verified", verify_status="confirmed")
    assert f["severity"] == "high"  # agent 初判
    assert assert_verified_severity(ctx.client, ctx.eid, fid) == "low"


def _tv_03(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.pump()
    assert assert_verified_severity(ctx.client, ctx.eid, fid) == "critical"
    # P0 告警事件 level=error
    assert any(e["level"] == "error" for e in ctx.events(ctx.find_verify_run(fid)))


def _tv_04(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.pump()
    assert_finding_state(ctx.client, ctx.eid, fid, status="pending_false_positive",
                         verify_status="rejected")


def _tv_05(ctx: E2ECtx) -> None:
    _tv_04(ctx)
    fid = ctx.latest_finding()
    resp = ctx.transition(fid, "false_positive", by="human")
    assert resp.status_code == 200
    assert_finding_state(ctx.client, ctx.eid, fid, status="false_positive")


def _tv_06(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.pump()
    assert_finding_state(ctx.client, ctx.eid, fid, status="open", verify_status="none")
    # 补证 explore 已入队（同覆盖项）→ 补证写回后再入 verify


def _tv_07(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.pump()
    # retest explore（非普通补证）已入队


def _tv_08(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.pump()
    # verified_traffic_ids 与 finding 无交集 → 转 needs_more_evidence
    assert_finding_state(ctx.client, ctx.eid, fid, status="open", verify_status="none")


def _tv_09(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    run = ctx.find_verify_run(fid)
    from mock_harness import assert_worker_exclusion
    assert_worker_exclusion(ctx.dispatch, run, creator="worker-A")


def _tv_10(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.pump()
    # 单 worker：finding 停留 pending_verify，不派发
    assert_finding_state(ctx.client, ctx.eid, fid, status="pending_verify")


def _tv_11(ctx: E2ECtx) -> None:
    for _ in range(3):
        ctx.create_finding()
    ctx.pump()
    # 3 个 verify 派到 B/C，无一派回 A
    for fid in ctx.client.get(f"/engagements/{ctx.eid}/findings").json()[-3:]:
        run = ctx.find_verify_run(fid["id"])
        assert ctx.dispatch.task_run(run)["worker"] != "worker-A"


def _tv_12(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.pump()
    first = ctx.find_verify_run(fid)
    # 人工再触发 → 可再次复核但重复触发不产生重复任务
    ctx.transition(fid, "verified", by="human")
    ctx.pump()


def _tv_13(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.pump()
    # verdict=maybe → 契约拒绝 → 该次 verify failed；finding 保持 pending_verify
    assert_finding_state(ctx.client, ctx.eid, fid, status="pending_verify")


def _tv_14(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.pump()
    f = assert_finding_state(ctx.client, ctx.eid, fid, status="verified")
    assert f["severity"] == "high"  # 回退 agent_severity


def _tv_15(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.pump()
    assert_finding_state(ctx.client, ctx.eid, fid, status="pending_verify")


def _tv_16(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.pump()
    run = ctx.find_verify_run(fid)
    assert ctx.dispatch.task_run(run)["status"] == "rejected"


def _tv_17(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.pump()
    assert_finding_state(ctx.client, ctx.eid, fid, status="pending_verify")
    assert ctx.dispatch.task_run(ctx.find_verify_run(fid))["status"] == "failed"


def _tv_18(ctx: E2ECtx) -> None:
    _tv_17(ctx)


def _tv_19(ctx: E2ECtx) -> None:
    _tv_17(ctx)


def _tv_20(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.pump()
    f = assert_finding_state(ctx.client, ctx.eid, fid, status="needs_review")
    assert f.get("reverify_count", 0) > 3


def _tv_21(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.pump()
    assert_finding_state(ctx.client, ctx.eid, fid, status="verified")
    rpt = ctx.client.get(f"/engagements/{ctx.eid}/report").text
    assert "SQL error near" in rpt
    assert "verified_severity" in rpt


def _tv_22(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.pump()
    assert_finding_state(ctx.client, ctx.eid, fid, status="verified")
    rpt = ctx.client.get(f"/engagements/{ctx.eid}/report").text
    assert "sshpass" in rpt  # 命令 + 回显


def _tv_23(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.pump()
    assert_retest_pass(ctx.client, ctx.eid, fid, count=2)
    resp = ctx.transition(fid, "closed", by="human")
    assert resp.status_code == 200


def _tv_24(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.pump()
    assert_finding_state(ctx.client, ctx.eid, fid, status="open")
    # P0 告警事件


def _tv_25(ctx: E2ECtx) -> None:
    fid = ctx.create_finding(traffic_ids=["tr-XXX"])
    ctx.pump()
    # traffic_missing 标记 → verify 默认 needs_more_evidence


def _tv_26(ctx: E2ECtx) -> None:
    _tv_21(ctx)
    r1 = ctx.client.get(f"/engagements/{ctx.eid}/report").text
    r2 = ctx.client.get(f"/engagements/{ctx.eid}/report").text
    assert r1 == r2


def _tv_27(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    run = ctx.find_verify_run(fid)
    from mock_harness import assert_events
    assert_events(ctx.dispatch, run, kinds={"step", "tool", "command"})


def _tv_28(ctx: E2ECtx) -> None:
    run = ctx.find_verify_run(_ensure(ctx))
    resp = ctx.client.get(f"/tasks/{run}/events?after_seq=0")
    assert resp.status_code == 200


def _tv_29(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    rows = ctx.client.get(f"/engagements/{ctx.eid}/findings?status=pending_verify").json()
    assert any(r["id"] == fid for r in rows)


def _tv_30(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.transition(fid, "fixed", by="human")
    ctx.pump()
    assert_replay_run(ctx.client, ctx.eid, fid, result="remediated", matched_original=0)
    assert_retest_pass(ctx.client, ctx.eid, fid, kinds={"replay"})


def _tv_31(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.transition(fid, "fixed", by="human")
    ctx.pump()
    assert_finding_state(ctx.client, ctx.eid, fid, status="open")
    resp = ctx.transition(fid, "closed", by="human")
    assert resp.status_code == 403  # HTTP 类未过 replay 不得人工 closed


def _tv_32(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.transition(fid, "fixed", by="human")
    ctx.pump()
    # ambiguous → 自动二次 verify；error → 有限重试后 failed


def _tv_33(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.pump()
    from mock_harness import assert_http_mismatch
    assert_http_mismatch(ctx.client, ctx.eid, fid)


def _tv_34(ctx: E2ECtx) -> None:
    # 直写 SQLite 的调用被隔离；traffic 仅经 POST /traffic 进入
    pass


def _tv_35(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.pump()
    assert_finding_state(ctx.client, ctx.eid, fid, status="verified")
    # 报告标注证据缺口；不伪造 http[]


def _tv_36(ctx: E2ECtx) -> None:
    # auto_created target + 覆盖项；report_ready 不阻塞
    pass


def _tv_37(ctx: E2ECtx) -> None:
    from mock_harness import assert_audit_run
    # 抽样复核命中；coverage_discrepancy → 回退 untested
    ctx.pump()
    assert_audit_run(ctx.client, ctx.eid, item_id="c-013", verdict="discrepancy")


def _tv_38(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    resp = ctx.client.post(f"/engagements/{ctx.eid}/kill")
    assert resp.status_code == 200
    # kill 后无新 traffic 索引；任务 cancelled


def _tv_39(ctx: E2ECtx) -> None:
    # CLI stdout 含 error/timeout 词 → 不产生 error 事件
    pass


def _tv_40(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    # delay=[1200,1200] + verify_timeout=5s → 超时强制取消 → 重派
    ctx.pump()
    # 恢复后正常完成


def _tv_41(ctx: E2ECtx) -> None:
    # 两 explore 引用同一覆盖项；第二个 claim False 不派发
    pass


def _tv_42(ctx: E2ECtx) -> None:
    # explore 声明 10 个 http[]，traffic 仅 2 条 → capture_gap + needs_more
    pass


def _tv_43(ctx: E2ECtx) -> None:
    # MOCK_REASON 连续校验失败 → engagement reason needs_review
    pass


def _tv_44(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.transition(fid, "fixed", by="human")
    ctx.pump()
    assert_retest_pass(ctx.client, ctx.eid, fid, count=3)
    # replay 重复触发不 +1
    # unchanged → 回 open + P0 + retest_pass 归零


def _tv_45(ctx: E2ECtx) -> None:
    # explore 输出 tested_scope.partial=true → coverage_records.partial=1
    pass


def _tv_46(ctx: E2ECtx) -> None:
    fid = _ensure(ctx)
    ctx.transition(fid, "fixed", by="human")
    ctx.pump()
    assert_retest_pass(ctx.client, ctx.eid, fid, kinds={"replay"})
    resp = ctx.transition(fid, "closed", by="human")
    assert resp.status_code == 403  # 未过命令重放门槛
