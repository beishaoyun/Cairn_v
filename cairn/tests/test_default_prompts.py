"""default prompt group 模板验收测试（prompt_group: default）。

契约来源：
- ``docs/prompts-pentest-templates.md`` §1-§8（真实 LLM 模板规格）；
- ``dispatcher/tasks/common.py`` 校验器（validate_bootstrap_payload /
  validate_reason_payload / validate_explore_payload / validate_verify_blind_payload /
  validate_verify_compare_payload / validate_replay_result）的**精确输出契约**；
- ``dispatcher/tasks/{bootstrap,reason,explore,verify,audit}.py`` 中
  ``build_*_prompt`` 实际渲染的占位符集合。

验收点：
1. ``prompts/default/`` 下 9 个模板文件齐全且非空；
2. 每个模板占位符与任务代码渲染一致（占位符集合 ⊆ 预期），且样例上下文渲染后无残留；
3. 渲染结果包含对应校验器要求的关键字段名 / 严格 JSON 提示；
4. ``prompt_group: default`` 可命中 default 组（config 默认值 + dispatch.example.yaml）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cairn.dispatcher.config import load, load_dict

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = (
    REPO_ROOT / "cairn" / "src" / "cairn" / "dispatcher" / "prompts" / "default"
)
EXAMPLE_YAML = REPO_ROOT / "dispatch.example.yaml"

#: 与 mock 组同名的模板文件清单
REQUIRED_FILES = [
    "bootstrap.md",
    "bootstrap_conclude.md",
    "reason.md",
    "explore.md",
    "explore_conclude.md",
    "verify_blind.md",
    "verify_comparison.md",
    "audit.md",
    "replay.md",
]

#: 每个模板允许出现的占位符（以任务代码 build_*_prompt / spec §7 为准）。
#: 模板中 JSON 示例的字面花括号（如 ``{"accepted": ...}``）不算占位符。
EXPECTED_PLACEHOLDERS: dict[str, set[str]] = {
    "bootstrap.md": {"origin", "goal", "hints", "scope"},
    "bootstrap_conclude.md": set(),
    "reason.md": {"graph_yaml", "gaps", "scope"},
    "explore.md": {
        "graph_yaml", "intent_id", "intent_description",
        "coverage_context", "traffic_ids", "scope",
    },
    "explore_conclude.md": {"intent_id", "intent_description", "coverage_context"},
    "verify_blind.md": {"traffic_digest", "scope"},
    "verify_comparison.md": {"observations", "finding", "traffic_digest", "scope"},
    "audit.md": {
        "item_id", "target_value", "target_id", "test_type_name",
        "test_type_id", "depth_required", "status", "scope",
    },
    "replay.md": {"trigger_traffic_id", "variants", "scope"},
}

#: 渲染后必须出现的校验器关键字段（裸字段名，字段说明/JSON 示例均含）
REQUIRED_FIELDS: dict[str, list[str]] = {
    "bootstrap.md": [
        "accepted", "data", "fact", "description", "sweep_complete",
        "discoveries", "coverage", "outcome", "target", "port", "service",
    ],
    "bootstrap_conclude.md": ["accepted", "data", "fact", "discoveries", "coverage", "outcome"],
    "reason.md": [
        "accepted", "data", "intents", "from", "description",
        "coverage_item_ids", "recommend_finalize", "reason", "waivers",
        "item_id", "kind", "not_applicable",
    ],
    "explore.md": [
        "accepted", "data", "description", "findings", "severity", "cvss_score",
        "cwe_id", "asset", "evidence_refs", "traffic_ids", "http", "method",
        "url", "response_status", "commands", "coverage", "covered_items",
        "depth_achieved", "outcome", "tested_scope", "partial",
    ],
    "explore_conclude.md": [
        "accepted", "data", "description", "findings", "coverage",
        "covered_items", "depth_achieved", "outcome", "tested_scope",
    ],
    "verify_blind.md": [
        "accepted", "data", "observations", "vuln", "severity",
        "traffic_id", "basis", "traffic_note",
    ],
    "verify_comparison.md": [
        "accepted", "data", "stage", "verdict", "verified_severity", "reason",
        "verified_traffic_ids", "http_mismatch", "suggested_action", "confirmed",
        "needs_more_evidence",
    ],
    "audit.md": [
        "accepted", "data", "description", "findings", "coverage",
        "covered_items", "depth_achieved", "outcome", "verdict",
        "match", "coverage_discrepancy",
    ],
    "replay.md": [
        "accepted", "data", "result", "matched_original", "remediated",
        "unchanged", "ambiguous", "error",
    ],
}

#: 样例上下文（渲染时替换占位符）
SAMPLE_CONTEXT: dict[str, str] = {
    "origin": "scope statement / 授权声明",
    "goal": "对授权目标集做初探",
    "hints": '["hint1", "hint2"]',
    "scope": "authorized: 10.0.0.0/24; prohibited: 10.0.0.1, 192.168.0.0/16",
    "graph_yaml": "id: f001\ndescription: recon done\nid: f002",
    "gaps": '[{"item_id": "c-013", "priority": 0.9, "target": "10.0.0.5"}]',
    "intent_id": "i-001",
    "intent_description": "对 10.0.0.5:8080 登录框做 SQL 注入测试",
    "coverage_context": '[{"item_id": "c-013", "target_value": "10.0.0.5", "test_type_name": "SQL注入", "depth_required": "deep"}]',
    "traffic_ids": '[{"id": "tr-001", "method": "POST", "url": "http://10.0.0.5:8080/login"}]',
    "traffic_digest": "### traffic tr-001\nPOST /login HTTP/1.1\n... [truncated, sha256=abc]",
    "observations": '[{"vuln": "SQL injection in /login", "severity": "high", "traffic_id": "tr-001", "basis": "SQL error echo"}]',
    "finding": '{"title": "SQLi", "severity": "high", "http": [{"method": "POST", "url": "http://10.0.0.5:8080/login"}]}',
    "item_id": "c-013",
    "target_value": "10.0.0.5",
    "target_id": "t-001",
    "test_type_name": "SQL注入",
    "test_type_id": "tt_web_sqli",
    "depth_required": "deep",
    "status": "tested_with_finding",
    "trigger_traffic_id": "tr-001",
    "variants": '["\'", " OR 1=1--"]',
}

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _read(name: str) -> str:
    path = PROMPTS_DIR / name
    assert path.is_file(), f"模板缺失: {path}"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"模板为空: {path}"
    return text


def _render(text: str, *, name: str) -> str:
    """用样例上下文替换模板占位符，返回渲染结果。"""
    for ph in EXPECTED_PLACEHOLDERS[name]:
        text = text.replace("{" + ph + "}", SAMPLE_CONTEXT[ph])
    return text


# ---------------------------------------------------------------------------
# 1. 文件齐全且非空；2. 占位符集合与代码渲染一致且渲染无残留
# ---------------------------------------------------------------------------


def test_all_required_files_exist_and_nonempty():
    for name in REQUIRED_FILES:
        assert (PROMPTS_DIR / name).is_file(), f"缺少模板文件: {name}"
        assert (PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


def test_placeholder_sets_match_code():
    """模板占位符 ⊆ 该任务的预期占位符（与 build_*_prompt 渲染一致）。"""
    for name in REQUIRED_FILES:
        text = _read(name)
        found = set(_PLACEHOLDER_RE.findall(text))
        expected = EXPECTED_PLACEHOLDERS[name]
        unexpected = found - expected
        assert not unexpected, f"{name} 出现未预期占位符: {sorted(unexpected)}"
        # 每个预期的占位符都应在模板中出现（bootstrap_conclude 无占位符，跳过）
        if expected:
            missing = expected - found
            assert not missing, f"{name} 缺少预期占位符: {sorted(missing)}"


def test_render_no_leftover_placeholders():
    """样例上下文渲染后，预期占位符全部被替换、无残留。"""
    for name in REQUIRED_FILES:
        text = _render(_read(name), name=name)
        leftover = {ph for ph in EXPECTED_PLACEHOLDERS[name] if "{" + ph + "}" in text}
        assert not leftover, f"{name} 渲染后仍有未替换占位符: {sorted(leftover)}"


# ---------------------------------------------------------------------------
# 3. 渲染结果包含校验器要求的关键字段 / 严格 JSON 提示 / 无 mock 机制
# ---------------------------------------------------------------------------


def test_rendered_contains_validator_fields():
    for name in REQUIRED_FILES:
        text = _render(_read(name), name=name)
        for field in REQUIRED_FIELDS[name]:
            assert field in text, f"{name} 渲染结果缺少字段 {field!r}"


def test_rendered_contains_strict_json_and_no_mock_markers():
    for name in REQUIRED_FILES:
        text = _render(_read(name), name=name)
        # 严格 JSON：只返回一个原始 JSON 对象
        assert "原始 JSON" in text, f"{name} 缺少严格 JSON 提示"
        # 无 mock 机制
        assert "mock-phase" not in text, f"{name} 含 mock-phase 标记"
        assert "mock-stage" not in text, f"{name} 含 mock-stage 标记"
        # 无 complete 字段（黄金不变量 5；bootstrap 用 sweep_complete）
        assert '"complete":' not in text, f"{name} 渲染结果含被禁止的 complete 字段"


def test_rendered_contains_scope_and_evidence_constraints():
    """约束注入：授权范围边界 + 禁止越界/DoS + 证据引用来自候选 traffic_ids（C5）。"""
    for name in REQUIRED_FILES:
        text = _render(_read(name), name=name)
        assert "授权" in text, f"{name} 缺少授权范围约束"
        assert "越界" in text, f"{name} 缺少越界请求约束"
        assert "DoS" in text, f"{name} 缺少 DoS 约束"
    # C5：explore 模板必须要求证据引用来自候选 traffic_ids（不能自查捕获索引）
    explore = _render(_read("explore.md"), name="explore.md")
    assert "traffic_ids" in explore and "C5" in explore


# ---------------------------------------------------------------------------
# 4. prompt_group: default 可命中 default 组
# ---------------------------------------------------------------------------


def _base_dict() -> dict:
    return {
        "server": {"url": "http://127.0.0.1:8000", "api_token": "${CAIRN_API_TOKEN}"},
        "runtime": {"execution": "container", "interval": 3},
        "workers": [{"name": "w1", "type": "mock", "task_types": ["bootstrap", "explore", "verify"]}],
    }


def test_prompt_group_defaults_to_default():
    """未显式指定 runtime.prompt_group → 默认 'default'（命中 default 组）。"""
    cfg = load_dict(_base_dict(), env={"CAIRN_API_TOKEN": "t"})
    assert cfg.runtime.prompt_group == "default"


def test_example_yaml_pins_default_group():
    """dispatch.example.yaml 显式声明 prompt_group: 'default'。"""
    cfg = load(EXAMPLE_YAML, env={"CAIRN_API_TOKEN": "t", "CAIRN_CAPTURE_TOKEN": "t"})
    assert cfg.runtime.prompt_group == "default"


def test_default_group_dir_matches_prompt_group_config():
    """prompt_group=default 时，prompts/default/ 目录存在且与 mock 组文件同名。"""
    assert PROMPTS_DIR.is_dir(), f"default prompt 组目录缺失: {PROMPTS_DIR}"
    mock_dir = PROMPTS_DIR.parent / "mock"
    mock_files = {p.name for p in mock_dir.glob("*.md")}
    default_files = {p.name for p in PROMPTS_DIR.glob("*.md")}
    assert mock_files == set(REQUIRED_FILES), f"mock 组文件与预期不符: {sorted(mock_files)}"
    assert default_files == set(REQUIRED_FILES), f"default 组文件与预期不符: {sorted(default_files)}"
    assert default_files == mock_files, "default 组与 mock 组模板文件名不一致"


# ---------------------------------------------------------------------------
# 5. 任务加载路径（build_*_prompt）样例上下文渲染不报错且输出符合契约
# ---------------------------------------------------------------------------


def test_task_prompt_builders_render_with_sample_context():
    """实际任务加载路径（build_*_prompt）用样例上下文渲染不报错、占位符被替换。"""
    from cairn.dispatcher.tasks.bootstrap import (
        build_bootstrap_conclude_prompt,
        build_bootstrap_prompt,
    )
    from cairn.dispatcher.tasks.reason import build_reason_prompt
    from cairn.dispatcher.tasks.explore import (
        build_explore_conclude_prompt,
        build_explore_prompt,
    )
    from cairn.dispatcher.tasks.verify import (
        build_verify_blind_prompt,
        build_verify_compare_prompt,
    )
    from cairn.dispatcher.tasks.audit import build_audit_prompt

    # bootstrap（占位符 origin/goal/hints/scope）
    p = build_bootstrap_prompt(origin="o", goal="g", hints=["h"], scope="s")
    assert '"accepted"' in p and '"sweep_complete"' in p and '"discoveries"' in p
    assert '"complete":' not in p

    p = build_bootstrap_conclude_prompt()
    assert '"accepted"' in p and '"fact"' in p

    # reason（占位符 graph_yaml/gaps/scope + 阈值）
    p = build_reason_prompt(graph_yaml="id: f001", gaps=[{"item_id": "c-013", "priority": 0.9}], scope="s")
    assert '"intents"' in p and '"coverage_item_ids"' in p and '"recommend_finalize"' in p
    assert '"complete":' not in p

    # explore（占位符 graph_yaml/intent_id/intent_description/coverage_context/traffic 候选/scope）
    p = build_explore_prompt(
        graph_yaml="id: f001", intent_id="i-001", intent_description="x",
        coverage_context=[{"item_id": "c-013", "target_value": "10.0.0.5"}],
        traffic_candidates=[{"id": "tr-001", "method": "POST", "url": "http://x/login"}],
        scope="s",
    )
    assert "tr-001" in p and '"covered_items"' in p and '"coverage"' in p
    assert '"complete":' not in p

    p = build_explore_conclude_prompt(intent_id="i-001", intent_description="x", coverage_context=[])
    assert '"coverage"' in p and '"description"' in p
    assert '"complete":' not in p

    # verify 两阶段（blind→comparison）
    p = build_verify_blind_prompt(traffic_digest="digest", scope="s")
    assert '"observations"' in p and '"traffic_note"' in p
    assert '"complete":' not in p

    p = build_verify_compare_prompt(
        observations=[{"vuln": "x"}], finding={"title": "x"},
        traffic_digest="digest", scope="s",
    )
    assert '"verdict"' in p and '"verified_severity"' in p and '"http_mismatch"' in p
    assert '"suggested_action"' in p and '"stage"' in p
    assert '"complete":' not in p

    # audit（explore 同构 + verdict）
    item = {
        "id": "c-013", "target_value": "10.0.0.5", "target_id": "t-001",
        "test_type_name": "SQL注入", "test_type_id": "tt_web_sqli",
        "depth_required": "deep", "status": "tested_with_finding",
    }
    p = build_audit_prompt(item=item, scope="s")
    assert '"verdict"' in p and '"coverage_discrepancy"' in p and '"covered_items"' in p
    assert '"complete":' not in p
