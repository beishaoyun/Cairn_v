# 31-mock-adapters 交接物

- 完成 Agent：31（mock adapters & regression base）  日期：2026-08-06
- 阶段：Phase 1 · 全链路验收使能者（mock 驱动 + 46 用例 runner）
- 依赖：12（config/客户端）、13（WorkerDriver 协议/registry）、21/22/23（服务端子域）。
  被依赖：30（tasks 校验器消费 mock 输出）、40（loop 装配 mock 驱动）、50（验收入口接线 e2e_ctx）。

## 1. 实现清单

| 文件 | 关键符号 | 说明 |
|---|---|---|
| `cairn/src/cairn/dispatcher/workers/adapters/mock.py` | `MockDriver`、`MOCK_ALLOWED_OUTCOMES`、`MOCK_DEFAULT_BEHAVIOR`、`MOCK_ALLOWED_ENV_KEYS`、`MOCK_EXTRA_KEYS`、`COVERAGE_OUTCOMES`、`validate_mock_config`、`validate_mock_extra_key`、`mock_env_key` | mock 驱动：seed session、`required_env_keys=()`、`check_health()=True`；`build_execute/conclude` 写 prompt 临时文件跑 `_mock_script.py`；构造时严格校验全部 `MOCK_*` env（rule 24） |
| `cairn/src/cairn/dispatcher/workers/adapters/_mock_script.py` | `main`、`detect_phase/stage`、`select_outcome`、`merge_payload`、`emit_*` | 可执行 worker 脚本（纯 stdlib，无 cairn import）；按 `mock-phase:`/`mock-stage:` 标记识别 phase/stage；rules 优先、概率兜底；延迟窗口；输出各 phase JSON 契约 |
| `cairn/src/cairn/dispatcher/workers/registry.py` | `get_driver_class`（加懒注册） | **对 13 的最小加性改动**：`get_driver_class("mock")` 首次查询时惰性注册 `MockDriver`（避免 registry 加载期 circular import）。不改变现有驱动行为。13 已预期此注册点 |
| `cairn/src/cairn/dispatcher/workers/adapters/__init__.py` | 导出 `MockDriver` | 31 所有（加法） |
| `cairn/src/cairn/dispatcher/prompts/mock/*.md` | 9 个模板 | mock prompt 组：bootstrap/bootstrap_conclude/reason/explore/explore_conclude/verify_blind/verify_comparison/audit/replay；每个含 `mock-phase`（verify 含 `mock-stage`）标记 + 结构化 JSON 输出契约 |
| `cairn/tests/mock_harness.py` | `mock_cfg`（`_phase/_verify/_replay/_explore/_reason/_bootstrap/_audit`）、`phase_cfg` 等、`mock_prompt`、`run_mock`、`make_mock_driver`、seed 辅助（`seed_traffic`/`seed_replay_evidence`/`seed_finding`）、断言辅助（`assert_finding_state` 等 §3.4 全部） | 两层：驱动/契约层（纯、独立可测）+ E2E 层（需 Server/Dispatcher，懒装配） |
| `cairn/tests/test_mock_end_to_end.py` | `TestMockDriver`/`TestMockScript*`/`TestMockHarness`（48 例）+ `TV_CASES`（46 例参数化）+ `e2e_ctx` fixture | 驱动单测永远可跑；TV-01..46 由 `e2e_ctx` 用 `pytest.importorskip` 保护，30/40 未就绪时全跳过 |
| `dispatch_mock.yaml` | 修正 `MOCK_*` env 为 spec 合规 outcome | **对共享配置的最小修正**：原文件用 v1 outcome 名（`complete`/`fact`/`intent`/`noop`/`invalid_payload`）与 verify-mock-test-spec §2 契约冲突，严格校验会拒绝；已改为 §2 允许的 outcome（bootstrap/reason/explore 组）。结构与字段未动 |

## 2. MOCK_ALLOWED_OUTCOMES 全表（verify-mock-test-spec §2.1 + 向后兼容）

| phase | 允许 outcome | 默认行为（`MOCK_DEFAULT_BEHAVIOR`） |
|---|---|---|
| healthcheck | `ok` `fail` `invalid_json` `empty` `command_fail` | delay [0.05,0.2]，ok=1.0（`fail` 为 v1 形状向后兼容） |
| bootstrap | `ok` `rejected` `invalid_json` `empty` `command_fail` | ok=1.0 |
| bootstrap_conclude | 同 bootstrap | ok=1.0 |
| reason | `intents` `finalize` `rejected` `invalid_json` `empty` `command_fail` | intents=1.0 |
| explore_execute | `fact` `rejected` `invalid_json` `empty` `command_fail` | fact=1.0 |
| explore_conclude | 同 explore_execute | fact=1.0 |
| **verify**（新增） | `confirmed` `rejected` `needs_more_evidence` `accepted_false` `invalid_json` `empty` `command_fail` | confirmed=1.0 |
| **replay**（新增） | `remediated` `unchanged` `ambiguous` `error` `invalid_json` `empty` `command_fail` | remediated=1.0 |
| **audit**（新增） | `covered` `discrepancy` `rejected` `invalid_json` `empty` `command_fail` | covered=1.0 |

- **meta outcomes**（invalid_json/empty/command_fail）每个 phase 通用：invalid_json→stdout `{invalid json` + exit 0；empty→空 stdout + exit 0；command_fail→exit 1（stderr 有提示）。
- `MOCK_ALLOWED_ENV_KEYS` 由 `MOCK_ALLOWED_OUTCOMES` 自动派生 → `MOCK_VERIFY`/`MOCK_REPLAY` 自动合法；另含 spec 定义的非 phase 键 `MOCK_EXPLORE_COVERAGE_OUTCOME`（prompts §9）。
- **校验（rule 24）**：未知 `MOCK_*` 键 / 未知 outcome / 概率非数 / 负概率 / 概率和≠1.0（Decimal 严格）/ delay 负或 [0]>[1] / payload 非 dict / rules 非 dict / rules.force 不在允许集 → `MockConfigError`（构造即抛）。

## 3. payload 注入（contract B）

- verify 输出 `{"accepted":true,"data":{"stage":"comparison","verdict":..., **payload}}`；`payload` 可含 `verified_severity`/`verified_traffic_ids`/`suggested_action`/`reason`/`http_mismatch`/`observations`/`traffic_note`。`payload.verdict` 可覆盖 `verdict`（TV-13 非法值注入）。`accepted_false` → `{"accepted":false,"reason":"mock_rejected"}`。
- replay 输出 `{"accepted":true,"data":{"result":..., "matched_original":..., **payload}}`；`matched_original` 默认 0。
- blind/comparison 两阶段区分：verify prompt 的 `mock-stage` 标记（blind→`observations`+`traffic_note`；comparison→verdict 家族）。`rules[].prompt_has` 可对两阶段分别注入（如 `{"prompt_has":"observations", ...}` 命中盲审）。
- explore 扩展（contract C）：`payload.findings`（含 http[]/commands[]/traffic_ids）+ `payload.coverage`；coverage.outcome 未 pin 时由 `MOCK_EXPLORE_COVERAGE_OUTCOME` 概率决定（缺省 no_issue）。

## 4. 46 用例 ↔ 规则映射核对

验证矩阵见 `verify-mock-test-spec.md §4`；本交接 `test_mock_end_to_end.py::TV_CASES` 逐条对应 TV 编号，`rules` 字段标注规则映射：

| 组 | 用例 | 规则映射 |
|---|---|---|
| A 基础 verdict | TV-01/02/03/04/05/06/07/08 | F1 三分支、双轨 severity、P0 告警、rejected→pending_false_positive（非终态）、needs_more→补证 explore、suggested_action=retest_now、traffic_ids 无交集契约拦截 |
| B 独立性派发 | TV-09/10/11/12 | 规则37（派发独立性）、worker≠创建者、单 worker 降级、去重键 finding_id+stage |
| C 契约与异常 | TV-13/14/15/16/17/18/19/20 | verdict/severity/traffic_ids 三入口校验、accepted=false、invalid_json/empty/command_fail 重试 ≤3、**规则28/F6** needs_more 循环超限→needs_review |
| D 全链路 | TV-21..26 | bootstrap→explore→verify→报告（含请求/响应包+verified_severity）、非 HTTP 命令回显、复测通过闭环（retest_pass≥2）、复测仍存在回 open+P0、traffic_missing、报告幂等 |
| E 进度联动 | TV-27/28/29 | task_runs/task_events 生命周期、SSE after_seq 增量、前端联动 |
| F 复测重放·捕获·协议边界 | TV-30/31/32/33/34/35 | **规则31** replay remediated/unchanged/ambiguous、**规则29/C2** http_mismatch、**规则32/F8** 代理单写者、**规则36/F10** 协议边界降级 |
| G 覆盖闭环·熔断采集 | TV-36..40 | **规则33/F11** auto_created、**规则34/F3** 抽样复核 discrepancy、**规则30/C3** kill 停捕获、**规则35/F9** 结构化流分类、挂起超时重派（delay=[1200,1200]+verify_timeout） |
| H 修复闭环新增 | TV-41..46 | **规则38/B1** 格子互斥、**规则40/C2** 捕获对账、**规则41/C8** reason 升级、**A2/C10** 复测账本幂等与归零、**C9** partial、**规则26** 非 HTTP 命令确定性重放 |

> 另：verify-mock-test-spec §4 G 组表下的规则映射注释（28→TV-20 · 29→TV-33 · 30→TV-38 · 31→TV-30/31/32 · 32→TV-34 · 33→TV-36 · 34→TV-37 · 35→TV-39 · 36→TV-35）与 H 组表下（37→TV-09/11 · 38→TV-41 · 40→TV-42 · 41→TV-43 · C10→TV-44 · C9→TV-45 · 26→TV-46）已逐一核对并在 `TV_CASES.rules` 标注。

## 5. 未通过 / 跳过用例清单（交给 50 复验）

- **46 例 TV-01..46 当前全部 SKIPPED**（`e2e_ctx` 对 `cairn.dispatcher.scheduler.loop` 与 `cairn.dispatcher.tasks.verify` 做 `pytest.importorskip`；30/40 尚未交付）。预期行为。
- 驱动/harness 单测 **48 passed**（`pytest cairn/tests/test_mock_end_to_end.py`）。
- **50 接线清单**：把 `test_mock_end_to_end.py::e2e_ctx` 替换为「进程内 Server TestClient + CairnClient + DispatcherLoop(LocalBackend + MockDriver)」装配（沿用 verify-mock-test-spec §3.1/§5 模式），worker-A 创建者 + worker-B/C 独立 verify。随后 TV 矩阵按 `rules` 标注逐条复验。
- **30 依赖状态**：30 的 `validate_verify_blind_payload`/`validate_verify_compare_payload`/`validate_replay_result`/`validate_explore_payload`/`validate_reason_payload` 尚未落地；mock 输出形状已按 skeleton §4 与 prompts §8 对齐，30 落地后应全部接受（若拒绝，属 30 校验器 bug，勿改 mock 掩盖——记录并回传）。

## 6. harness 用法

```python
from mock_harness import (
    mock_cfg, phase_cfg, verify_cfg, replay_cfg, explore_cfg,
    make_mock_driver, run_mock, mock_prompt, parse_mock_json,
)

# 1) 单 worker 覆盖多场景：MOCK_VERIFY + rules[].prompt_has 区分 blind/comparison
cfg = verify_cfg(outcome="confirmed", severity="high",
                 rules=[{"prompt_has": "critical-finding",
                         "force": "confirmed",
                         "payload": {"verified_severity": "critical"}}])
d = make_mock_driver(verify=cfg)
r = run_mock(d, mock_prompt("verify", stage="comparison", text="verdict critical-finding"))
assert parse_mock_json(r)["data"]["verified_severity"] == "critical"

# 2) 确定性 replay 结果注入（worker='replay-engine'）
d2 = make_mock_driver(replay=replay_cfg(result="unchanged", matched_original=2))

# 3) explore 出带 traffic_ids 的 HTTP finding（全链路 TV-21/22 前置）
d3 = make_mock_driver(explore_execute=explore_cfg(
    findings=[{"title": "SQLi", "severity": "high", "asset": "http://10.0.0.5/login",
               "traffic_ids": ["tr-001"], "http": [{"method": "POST", "url": "...",
               "request_body": "u=' OR 1=1--", "response_status": 200, "response_body": "SQL error"}]}],
    coverage={"covered_items": ["c-013"], "depth_achieved": "standard", "outcome": "finding_created"}))
```

- 序列化：`mock_cfg.env(phase, cfg)` → JSON 字符串；`mock_cfg.worker_env(verify=..., replay=...)` → `{MOCK_VERIFY:..., MOCK_REPLAY:...}`。
- 种子：`seed_traffic(client, eid, "tr-001")`、`seed_replay_evidence(client, eid)`（预置 tr-101 role=replay）、`seed_finding(client, eid, ...)`。
- 时序确定性：所有 harness cfg 默认 `delay=[0.01,0.01]`；要特定 verdict 用 `rules[].force` + `payload`，不依赖概率（§6）。

## 7. 未实现 / 待定 / 已知注意

- **e2e_ctx 接线**由 50 完成（本包只写 runner 与保护）。
- **replay 引擎 `compare_signature`**（status+body 指纹纯函数）属 30，单测在 30；mock 只注入结果。
- **container 模式下 mock 不可真用**：`build_execute` 用宿主 `sys.executable` 跑本地脚本路径，`docker exec` 内不可达。mock 是测试/dry-run 驱动，仅 local 执行（测试/演练）。文档化于此。
- **`MOCK_HEALTHCHECK` 的 `fail`** 仅向后兼容 v1 形状；v2 健康由 `MockDriver.check_health()=True` 负责，不消费该 phase。
- **prompt 临时文件**：`MockDriver.build_execute` 写 `tempfile.TemporaryDirectory`（驱动实例生命周期），脚本读后自删；进程被强杀时残留极少，GC 回收。
- **未 git commit**（按编排要求）；对 13 的 `registry.py` 与共享 `dispatch_mock.yaml` 做了最小加性/修正改动，已在 §1 标注。

## 8. 自测结果

- `pytest cairn/tests/test_mock_end_to_end.py` → **48 passed / 46 skipped**（skipped 全为 30/40 未就绪的 TV 矩阵）。
- `dispatch_mock.yaml` 加载 + `build_drivers` → 2 个 MockDriver 构造成功、`check_health()=True`（修正前会 MockConfigError）。
- 13/12 相关回归：`test_worker_drivers.py` + `test_dispatch_cli.py` + `test_dispatcher_config.py` → **54 passed**。
- 全量 `pytest` 待 50 处复跑核对（本包改动不触碰 server/services；registry 改动经 13 测试验证无回归）。
