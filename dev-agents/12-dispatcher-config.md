# Agent 12 — Dispatcher 配置层 + Cairn API 客户端

> 阶段 0 · 可与 10/11 并行。你的产出让 30-dispatcher-tasks 和 40-dispatcher-loop 能直接开工。

## 0. 开工前必读
1. `CLAUDE.md`（黄金不变量 1/2/10）
2. `docs/dispatch-config-spec.md` —— **全文（你的规格）**
3. `docs/backend-module-skeleton.md` §1（dispatcher 目录）、§2（API 契约清单）、§3（服务签名——客户端映射这些服务）、§4（校验器）
4. `docs/architecture-research-report-pentest-v2.md` §7（错误码/响应格式）、§8.2
5. `docs/verify-mock-test-spec.md` §2（MOCK_* 环境变量与 TaskType 语义）

## 1. 交付范围
```
cairn/src/cairn/dispatcher/__init__.py
cairn/src/cairn/dispatcher/config.py        # dispatch.yaml 加载/校验/默认值合并
cairn/src/cairn/dispatcher/protocol/__init__.py
cairn/src/cairn/dispatcher/protocol/client.py  # CairnClient：Bearer + 全部需要的 Server 端点
cairn/src/cairn/dispatcher/errors.py         # 派发侧错误码（映射服务端 error_code）
cairn/src/cairn/dispatcher/contracts.py      # TaskType / 契约类型（若需）
cairn/tests/test_dispatcher_config.py
cairn/tests/test_protocol_client.py
```

## 2. 必须满足的契约
### config.py
- **Schema**：严格按 `dispatch-config-spec.md` §1-§9。顶层 `server/common_env/runtime/tasks/security/scope/tuning/container/workers`；用 dataclass 建模，缺省值对齐 spec（如 `interval=3`、`network_mode="bridge"`、`verify_eligible=true`、`writeback_retries=1`）。
- **TaskType**：`Literal["bootstrap","reason","explore","verify","audit","replay"]`；**replay 是引擎任务**（`worker="replay-engine"`），不在 workers 的 task_types 校验范围。
- **${ENV_VAR} 展开**：字符串值内 `${VAR}` 在加载时展开（未设置→启动报错并指明变量名）；`api_token`/worker `env` 里的密钥**必须**走展开，禁止明文加载成功。
- **校验失败**：明确报错（缺必填段、task_type 非法、network_mode 非法、workers 重名等），不静默 fallback。
- **merge 语义**：`common_env` 与 per-worker `env` 合并（per-worker 优先）；scope_policy 同名键可覆盖 tuning 默认（记录于 spec §10，不在本层强制，仅透传）。

### protocol/client.py
- 单 `CairnClient(base_url, token)`，Bearer 头；所有响应按 v2 §7.3 错误码解析，非 2xx 抛带 error_code 的异常。
- 方法面（对应 skeleton §2，服务端子域 20-24 会实现这些端点；**客户端先按契约写，联调在阶段 2**）：
  - engagement：`list_active()`（含 scope/kill 状态）、`get(eid)`、`set_status(eid, status, retest=)`、`kill(eid)`
  - scope/targets：`list_targets(eid)`、`create_target(eid, ...)`、`check_scope(eid, value)`（SCOPE_DENIED 语义）
  - coverage：`get_coverage(eid)`、`get_gaps(eid, exclude_in_progress=)`、`list_items(eid)`、`waive(eid, item_id, kind, reason)`、`write_coverage_result(eid, ...)`（= 服务端 coverage 写回，含幂等键）
  - graph：`export_yaml(pid)`、`claim_intent(pid, ...)`、`heartbeat_intent(pid, iid)`、`release_intent(pid, iid)`、`conclude_intent(pid, iid)`
  - findings：`create_finding(eid, payload)`（agent 只能 open）、`upload_evidence(fid, file, kind)`、`add_http_evidence(fid, http_obj)`、`add_command_evidence(fid, cmd)`、`link_traffic(fid, traffic_ids, role)`
  - traffic：`list_traffic(eid, client=, since=)`、`resolve_traffic(eid, tid, for_model=True)`（digest）
  - progress：`open_task_run(eid, project_id=, task_type, worker)`、`append_event(run_id, kind, level, message, raw_path=)`、`finish_task_run(run_id, status, outcome_note=)`
  - report/audit：`trigger_audit(eid, item_id)`、`get_report(eid)`

## 3. 验收标准
1. `pytest test_dispatcher_config.py`：解析三份示例 yaml（`dispatch.example.yaml`/`dispatch_mock.yaml`/`dispatch.local.example.yaml`）不报错；${ENV_VAR} 未设报错；非法 task_type/network_mode 报错；`dispatch.local.example.yaml` 无 `container` 段合法（local 模式）。
2. `pytest test_protocol_client.py`：用 FastAPI TestClient 起一个 stub server（只实现错误码/健康/回显），验证客户端：401 抛 AUTH、业务 409 抛对应 error_code、Bearer 头正确、digest/export 参数透传。
3. 客户端方法名与 skeleton §3 服务签名一一可映射（自查清单写入交接物）。

## 4. 硬约束
- 客户端**不缓存**任何 Server 数据；每次调用走 HTTP。进度/心跳例外见 40。
- **不实现任何任务逻辑**（那是 30）；不实现调度循环（那是 40）。
- config schema 与 `dispatch-config-spec.md` 冲突时，**以 spec 为准**并同步 spec（先列 diff）。

## 5. 交接物
写 `dev-agents/notes/12-dispatcher-config.md`：config 默认值表、客户端方法清单（= skeleton §3 映射表）、stub server 测试方法、留给 30/40 的注意事项。
