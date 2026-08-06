"""12-dispatcher-config 验收测试：dispatch.yaml 解析/校验/${ENV_VAR} 展开/merge 语义。

覆盖 dev-agents/12-dispatcher-config.md §3.1：
- 三份示例 yaml（dispatch.example.yaml / dispatch_mock.yaml / dispatch.local.example.yaml）解析不报错；
- ${ENV_VAR} 未设报错（指明变量名）；
- 非法 task_type / network_mode 报错；
- local 模式无 container 段合法；
- 默认值对齐 spec（interval=3 / network_mode=bridge / verify_eligible=true / writeback_retries=1）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cairn.dispatcher.config import ConfigError, load, load_dict, loads
from cairn.dispatcher.contracts import WORKER_TASK_TYPES

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_YAML = REPO_ROOT / "dispatch.example.yaml"
MOCK_YAML = REPO_ROOT / "dispatch_mock.yaml"
LOCAL_YAML = REPO_ROOT / "dispatch.local.example.yaml"

#: 示例配置里唯一被 ${ENV_VAR} 引用的变量
ENV = {
    "CAIRN_API_TOKEN": "test-token-123",
    "CAIRN_CAPTURE_TOKEN": "test-capture-token",
}


def base_dict() -> dict:
    """一份最小合法配置（无 common_env/tasks/security/scope/tuning/container/local 段）。"""
    return {
        "server": {"url": "http://127.0.0.1:8000", "api_token": "${CAIRN_API_TOKEN}"},
        "runtime": {"execution": "container", "interval": 3},
        "workers": [{"name": "w1", "type": "mock", "task_types": ["bootstrap", "explore", "verify"]}],
    }


# ---------------------------------------------------------------------------
# 三份示例 yaml 解析
# ---------------------------------------------------------------------------


def test_parse_example_yaml():
    cfg = load(EXAMPLE_YAML, env=ENV)
    assert cfg.server.url == "http://cairn-server:8000"
    assert cfg.server.api_token == "test-token-123"
    assert cfg.runtime.interval == 3
    assert cfg.runtime.execution == "container"
    assert cfg.container.network_mode == "bridge"
    assert cfg.tuning.writeback_retries == 1
    assert cfg.scope.enforce_kill_switch is True
    assert cfg.tasks.replay.timeout == 60

    w0 = cfg.workers[0]
    assert w0.name == "claudecode_deepseek-v4-pro"
    assert w0.verify_eligible is True
    assert w0.effective_env(cfg.common_env)["ANTHROPIC_MODEL"] == "deepseek-v4-pro"
    # 与 common_env 合并，per-worker 优先
    assert w0.effective_env({"ANTHROPIC_MODEL": "common-model"})["ANTHROPIC_MODEL"] == "deepseek-v4-pro"


def test_parse_mock_yaml():
    cfg = load(MOCK_YAML, env=ENV)
    assert cfg.runtime.prompt_group == "mock"
    assert len(cfg.workers) == 2
    for w in cfg.workers:
        assert w.type == "mock"
        assert set(w.task_types) <= set(WORKER_TASK_TYPES)
        assert "verify" in w.task_types and "audit" in w.task_types
        assert "MOCK_VERIFY" in w.env and "MOCK_REPLAY" in w.env
        assert w.verify_eligible is True


def test_parse_local_yaml_no_container_section():
    cfg = load(LOCAL_YAML, env=ENV)
    assert cfg.runtime.execution == "local"
    assert cfg.runtime.worker_healthcheck == "disabled"
    # 无 container 段 → 使用容器段默认值（本地模式合法）
    assert cfg.container.network_mode == "bridge"
    assert cfg.container.image
    assert cfg.local.completed_action == "keep"
    assert cfg.local.workspace_root is None


def test_loads_from_string():
    cfg = loads(
        "server:\n  url: http://x\n  api_token: ${CAIRN_API_TOKEN}\n"
        "runtime:\n  execution: local\n"
        "workers:\n  - name: a\n    type: mock\n    task_types: [bootstrap]\n",
        env=ENV,
    )
    assert cfg.runtime.execution == "local"
    assert cfg.workers[0].name == "a"


# ---------------------------------------------------------------------------
# ${ENV_VAR} 展开
# ---------------------------------------------------------------------------


def test_env_var_unset_raises_naming_variable():
    with pytest.raises(ConfigError) as exc:
        load(EXAMPLE_YAML, env={})
    assert "CAIRN_API_TOKEN" in str(exc.value)


def test_api_token_plaintext_rejected():
    raw = base_dict()
    raw["server"]["api_token"] = "sk-plaintext"
    with pytest.raises(ConfigError, match="api_token"):
        load_dict(raw, env=ENV)


def test_api_token_expanded_empty_rejected():
    raw = base_dict()
    with pytest.raises(ConfigError, match="api_token"):
        load_dict(raw, env={"CAIRN_API_TOKEN": ""})


def test_env_expansion_in_worker_env():
    raw = base_dict()
    raw["workers"][0]["env"] = {"MOCK": "${MOCK_CFG}"}
    cfg = load_dict(raw, env={**ENV, "MOCK_CFG": "{}"})
    assert cfg.workers[0].env["MOCK"] == "{}"


# ---------------------------------------------------------------------------
# 校验失败
# ---------------------------------------------------------------------------


def test_missing_required_server_section():
    raw = base_dict()
    del raw["server"]
    with pytest.raises(ConfigError, match="server"):
        load_dict(raw, env=ENV)


def test_missing_required_runtime_section():
    raw = base_dict()
    del raw["runtime"]
    with pytest.raises(ConfigError, match="runtime"):
        load_dict(raw, env=ENV)


def test_missing_required_workers_section():
    raw = base_dict()
    del raw["workers"]
    with pytest.raises(ConfigError, match="workers"):
        load_dict(raw, env=ENV)


def test_unknown_top_level_section_rejected():
    raw = base_dict()
    raw["serverr"] = {}
    with pytest.raises(ConfigError, match="顶层段"):
        load_dict(raw, env=ENV)


def test_worker_task_type_replay_rejected():
    raw = base_dict()
    raw["workers"][0]["task_types"] = ["bootstrap", "replay"]
    with pytest.raises(ConfigError, match="replay"):
        load_dict(raw, env=ENV)


def test_worker_task_type_invalid_rejected():
    raw = base_dict()
    raw["workers"][0]["task_types"] = ["bootstrap", "hack"]
    with pytest.raises(ConfigError, match="task_types"):
        load_dict(raw, env=ENV)


def test_invalid_network_mode_rejected():
    raw = base_dict()
    raw["container"] = {"network_mode": "nat"}
    with pytest.raises(ConfigError, match="network_mode"):
        load_dict(raw, env=ENV)


def test_duplicate_worker_name_rejected():
    raw = base_dict()
    raw["workers"].append({"name": "w1", "type": "mock", "task_types": ["reason"]})
    with pytest.raises(ConfigError, match="重名"):
        load_dict(raw, env=ENV)


def test_invalid_execution_mode_rejected():
    raw = base_dict()
    raw["runtime"]["execution"] = "docker"
    with pytest.raises(ConfigError, match="execution"):
        load_dict(raw, env=ENV)


def test_unknown_task_key_in_tasks_rejected():
    raw = base_dict()
    raw["tasks"] = {"nonsense": {"timeout": 1}}
    with pytest.raises(ConfigError, match="未知任务"):
        load_dict(raw, env=ENV)


# ---------------------------------------------------------------------------
# 默认值 / merge 语义
# ---------------------------------------------------------------------------


def test_defaults_on_minimal_config():
    cfg = load_dict(base_dict(), env=ENV)
    assert cfg.runtime.interval == 3
    assert cfg.runtime.max_workers == 8
    assert cfg.runtime.worker_healthcheck == "startup_only"
    assert cfg.container.network_mode == "bridge"
    assert cfg.container.completed_action == "stop"
    assert cfg.tuning.writeback_retries == 1
    assert cfg.security.static_encryption is True
    assert cfg.workers[0].verify_eligible is True
    assert cfg.tasks.bootstrap.timeout == 300
    assert cfg.tasks.bootstrap.conclude_timeout == 90
    assert cfg.tasks.reason.max_intents == 2
    assert cfg.tasks.replay.timeout == 60


def test_common_env_and_worker_env_merge_per_worker_wins():
    raw = base_dict()
    raw["common_env"] = {"A": "1", "B": "common", "PROXY": "http://common:8080"}
    raw["workers"][0]["env"] = {"B": "worker", "MODEL": "m1"}
    cfg = load_dict(raw, env=ENV)
    effective = cfg.workers[0].effective_env(cfg.common_env)
    assert effective["A"] == "1"
    assert effective["B"] == "worker"  # per-worker 优先
    assert effective["PROXY"] == "http://common:8080"
    assert effective["MODEL"] == "m1"


def test_tasks_section_overrides_defaults():
    raw = base_dict()
    raw["tasks"] = {"explore": {"timeout": 600, "conclude_timeout": 120}, "verify": {"timeout": 120}}
    cfg = load_dict(raw, env=ENV)
    assert cfg.tasks.explore.timeout == 600
    assert cfg.tasks.explore.conclude_timeout == 120
    assert cfg.tasks.verify.timeout == 120
    # 未声明的任务保留 spec 默认
    assert cfg.tasks.bootstrap.timeout == 300
