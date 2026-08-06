# 30-dispatcher-tasks 交接物

- 完成 Agent：30-dispatcher-tasks  日期：2026-08-06
- 阶段：Phase 1 · 与 20-24 服务端子域并行。依赖 12（客户端）/ 13（runtime 协议）。
- 交付：
  - `dispatcher/tasks/{common,bootstrap,reason,explore,verify,audit}.py` + `__init__.py`
  - `dispatcher/findings/writer.py`（`FindingsWriter`）
  - `dispatcher/coverage/writer.py`（`CoverageWriter`）
  - `dispatcher/replay/engine.py`（`ReplayEngine`，F4）
  - `dispatcher/progress/stream.py`（F9）
  - `cairn/tests/test_tasks.py`（**70 passed**）
- 自测：`uv run --project cairn pytest cairn/tests/test_tasks.py` → **70 passed**；
  全量 `pytest -q` → **425 passed / 46 skipped**（46 skipped 为 docker 环境跳过等环境性 skip，
  无失败）。

## 1. 任务 → 校验器 → 写回映射表

| 任务 | 校验器（skeleton §4） | 写回（经 12 CairnClient，C5） | 状态枚举/错误 |
|---|---|---|---|
| bootstrap | `validate_bootstrap_payload`（fact+sweep_complete+discoveries；禁 `complete`） | `CoverageWriter.seed_from_discovery`（check_scope→create_target→POST /coverage/items） | success/failed/cancelled |
| reason | `validate_reason_payload`（intents 引用 gaps、from⊆facts、收敛硬约束、禁 complete） | 不写回（intents 由 40 建图）；C8 升级计数给 40 持久化 | success/failed（`REASON_CONVERGENCE`+escalate） |
| explore | `validate_explore_payload`（description+findings[]+coverage 必填+交叉校验）→ `validate_findings_payload` + `validate_coverage_result` | B1 claim/release → `write_coverage_result`（幂等键 item_id:intent_id）→ `FindingsWriter.write`（create_finding+http/commands+link_traffic trigger） | success/retryable（`COVERAGE_ALREADY_COVERED`）/failed |
| verify | `validate_verify_blind_payload`（observations）/ `validate_verify_compare_payload`（stage=comparison+verdict/severity/reason/traffic_ids/http_mismatch） | `POST /findings/{fid}/verify`（22 apply_verify_runs；http_mismatch 在任务内比对降级 needs_more） | success/failed |
| audit | 复用 `validate_explore_payload` + `verdict` ∈ {match,coverage_discrepancy} | `POST /coverage/items/{cid}/audit`（21 apply_audit_verdict） | success/failed |
| replay（引擎） | `validate_replay_result`（matched_original+result 枚举） | `POST /findings/{fid}/replay`（登记）+ `record_retest_confirmation(kind='replay')`（remediated 时） | 返回 result dict |

**`complete` 一律拒绝**：`parse_accepted` → `_reject_complete` 递归拒绝任何层级 `complete`；
bootstrap 用 `sweep_complete`（初探完成）。findings 数组内嵌套 `complete` 也被拒。

## 2. 关键实现说明

### 校验器（tasks/common.py，全部实现）
- `validate_reason_payload` 收敛硬约束：`high_priority_gaps=True` 且无 intents 且无
  `recommend_finalize` → `PayloadError`（任务失败 + escalate）。
- `validate_coverage_result`：`outcome=no_issue` 必须声明 `tested_scope`（C9，与 21 服务端同口径）；
  `not_applicable` 只建议不置状态（B4，由服务端落实）。
- `validate_verify_blind_payload`：`observations` 必须存在且为数组（**可为空**，诚实负面合法，
  兼容 prompts §4.1「observations: [] 是合法答案」）。
- `validate_findings_payload`：severity 白名单 / cvss∈[0,10] / CWE-\d+ / evidence_refs 相对路径
  （拒绝对路径/`..`）/ http method/url/status / commands command 必填。

### 执行编排（common.run_worker_phase，13 §7）
`prepare_session → driver.build_execute → backend.build_exec_process(timeout=tasks.*.timeout) →
cancellation.attach_process → communicate → extract_session → extract_response_text`；取消/kill
switch 即杀进程。二阶段收尾走 `run_conclude_phase`（build_conclude）。

### explore traffic_ids 注入（C5）
`collect_traffic_candidates(ctx, worker=ctx.worker, since=intent_start)` → `client.list_traffic(eid,
client=<worker>, since=...)` → 渲染进 explore prompt 的 `{traffic_candidates}`；Agent 只从候选引用，
不能自查捕获索引。写回时 `link_traffic(role='trigger', source='captured')`。

### 写回重试（G）
`with_retry`（common.py）：findings/coverage 写失败退避 1 次（`tuning.writeback_retries`，经
`writeback_retries(ctx)` 读取），仍失败只记日志/抛 `WritebackError`；确定性拒绝（4xx /
`NON_RETRYABLE_CODES`）不重试。覆盖写回幂等键 = `{item_id}:{intent_id}`（`Idempotency-Key` 头，
服务端 `(item_id, intent_id)` 去重）。

### replay 引擎（F4，replay/engine.py）
- `compare_signature`（纯函数）：status + body 指纹（归一化空白后 sha256）比对 → `{matched,
  status_match, body_match, orig_fingerprint, now_fingerprint}`。
- `replay_http`：解析 `resolve_traffic(for_model=False)` 全量 → 原始包 + payload 变体经捕获代理
  （`httpx.Client(proxy=...)` 或注入 http_client）发送 → 签名比对。`matched_original>0`→unchanged；
  `matched=0` 且 status 同 body 异→ambiguous；否则 remediated。
- `replay_command`（非 HTTP 类，capture §6.1）：受控 wrapper `subprocess.run(shell=True)` 抓真实
  stdout/stderr + sha256，判定签名。
- `remediated` → `record_retest_confirmation(kind='replay')`（22 幂等，TV-44）。

### F9 流分类（progress/stream.py）
`classify_line(line, stream=stdout|stderr)`：结构化 JSON（`type` 字段）→ 映射 kind；
`$ `→command；`⚑ `→status；工具调用行→tool；stderr 流/traceback/`command not found`→error；
**stdout 含 "error"/"failed" 字样不算 error**（F9 防噪声）。`summarize_event` ≤512B（为省略号
预留字节，编码后严格 ≤max_bytes）。

## 3. 给 40 的调用接口（函数签名 + 返回值）

```python
# TaskContext（common.py）—— 40 装配
@dataclass
class TaskContext:
    client: CairnClient
    config: DispatcherConfig
    cancellation: TaskCancellation | None = None
    worker: str = ""
    eid: str = ""
    project_id: str | None = None
    run_id: str | None = None
    log: Callable[[str], None] = ...

# TaskResult（common.py）
@dataclass
class TaskResult:
    status: str            # success|failed|cancelled|rejected|retryable
    data: Any = None
    error: str | None = None
    error_code: str | None = None
    outcome_note: str | None = None
    escalate: bool = False # reason C8 升级信号
    extra: dict = {}

# 单阶段执行（40 若想自己驱动也可用）
run_worker_phase(ctx, *, driver, backend, prompt, timeout=None, session_id=None) -> (text, sid)
run_conclude_phase(ctx, *, driver, backend, prompt, timeout=None, session_id=None) -> (text, sid)

# 任务函数（driver/backend 由 40 传入；timeout 内部取 tasks.*.timeout）
run_bootstrap(ctx, *, driver, backend, origin, goal, hints=None, scope="", task_cfg=None) -> TaskResult
run_bootstrap_conclude(ctx, *, driver, backend, session_id=None, task_cfg=None) -> TaskResult
run_reason(ctx, *, driver, backend, gaps, graph_yaml, scope="", task_cfg=None,
           min_priority_threshold=0.30) -> TaskResult
    # gaps = client.get_gaps(eid, exclude_in_progress=True, limit=50)（21 裸 list）
    # data = {"intents":[...], "coverage":{...}, "gaps":[...]}
    # 失败 escalate=True → 40 用 ReasonEscalation 计数持久化 scheduler_state
run_explore(ctx, *, driver, backend, intent, graph_yaml, scope="", task_cfg=None,
            traffic_since=None, claimed_item_ids=None) -> TaskResult
    # intent = {"id","description","coverage_item_ids","from_fact_ids"}; claimed_item_ids 由 40
    # 派发前 B1 认领传入（未传则本函数自行认领）；claimed=false → 不派发
run_explore_conclude(ctx, *, driver, backend, intent, session_id=None, task_cfg=None,
                     claimed_item_ids=None) -> TaskResult
run_verify(ctx, *, driver, backend, finding, eid, scope="", verify_policy=None, task_cfg=None,
           independence="cross_worker", max_reverify=3) -> TaskResult
    # finding 含 id/title/severity/traffic_links/http_evidence；independence 由 40 依派发传入
run_audit(ctx, *, driver, backend, item, scope="", task_cfg=None, reason="sampling",
          auditor=None) -> TaskResult

# verify 派发选择函数（40 接入 loop）
select_verify_worker(creator_worker, workers, *, independence="cross_worker") -> str | None
    # workers = [WorkerConfig]; 排除创建者 + verify_eligible；无独立 worker 返回 None（降级 cross_run）

# C8 reason 升级计数（40 持久化）
ReasonEscalation(max_consecutive_failures=3, max_finalize_rejected=3)
    .record_failure(eid, *, finalize_rejected=False) -> dict   # 超限置 escalated=True
    .snapshot(eid) / .load(eid, state) / .reset(eid)
    escalation_state_key(eid) -> "reason_escalation:{eid}"     # scheduler_state key

# replay 引擎（worker='replay-engine'，不占 worker 并发）
ReplayEngine(client, *, retries=2, proxy=None, http_client=None, log=None)
    .run(eid, fid, *, trigger_traffic_id, payload_variants=0, is_http=True) -> dict
    #   result ∈ {unchanged, remediated, ambiguous, error}; remediated → 自动记 retest(kind=replay)
    .compare_signature(now_resp, orig_resp) -> dict            # 纯函数（单测焦点）
    .replay_http(full, *, variants=[]) -> dict
    .replay_command(command) -> dict
    .record_retest_confirmation(eid, fid, *, kind, note=None, actor="replay-engine")

# 写回器（40/30 通用）
CoverageWriter(client, *, retries=1, backoff=0.5, log=None)
    .claim_item(eid, item_id, intent_id) -> bool               # B1
    .release_item(eid, item_id, intent_id) -> None
    .claim_all(eid, item_ids, intent_id) -> (claimed_ids, busy_ids)
    .write_result(eid, *, item_ids, depth_achieved, outcome, intent_id, fact_id=None,
                  evidence_refs=None, tested_scope=None, partial=False) -> dict   # C9 幂等
    .seed_from_discovery(eid, discoveries, *, scope=None) -> dict  # bootstrap 播种
FindingsWriter(client, *, retries=1, backoff=0.5, log=None)
    .write(eid, *, findings, detected_by, actor="agent", source_fact_id=None,
           coverage_item_id=None) -> list[dict]                # FINDING_DUP → 追加证据不重复建单

# F9 流分类（40 drain 线程接线）
classify_line(line, *, stream="stdout") -> (kind, level)
classify_stream(lines, *, stream="stdout") -> iterator[(kind, level, message)]
summarize_event(message, *, max_bytes=512) -> str
EventStream(task_run_id, raw_dir=None).emit(kind, level, message) -> append_event 入参
```

## 4. 未实现 / 待定（留给 40/阶段 2）

- **调度循环 / worker 选择 / 心跳 / 进度 drain**：属 40，本包只提供单任务纯函数 + 选择函数
  （`select_verify_worker`）。
- **intent 图持久化**（reason 产出 intents → `create_intent`/`claim_intent`/conclude）：25 图子域
  端点为「路径假设」，客户端无 `create_intent` 方法。本包 reason/explore 只产出/消费 intent dict；
  **40 负责建 intent、claim 覆盖项（B1）后派发 explore**。
- **C8 计数持久化**：`ReasonEscalation` 计数器在本包，**持久化到 scheduler_state 由 40 落库**
  （key `reason_escalation:{eid}`；21 只读判定，服务端无写端点）。
- **replay 命令重放的「受控执行器」**：当前 `replay_command` 用 `subprocess.run(shell=True)` 本地
  wrapper；capture §6.1 的「受控执行器通道」（不经过 Agent 会话、沙箱内执行）需 11/40 接入
  `executor_url` 后路由。判定逻辑/账本已就绪。
- **replay_runs 结果回写端点**：22 只登记 queued 行；result 回写无独立端点（`_record_replay_run`
  目前 best-effort 登记），阶段 2 联调对齐。
- **`resolve_traffic` for_model 默认值**：客户端默认 True（digest）；replay/verify 全量比对显式
  传 `for_model=False`（本包已按此实现）。
- **scheduler_state / coverage claim-replay 端点**：`claim`/`release`/`audit`/`retest`/`verify`/
  `/coverage/items` 均走 `client._request`（路径假设，阶段 2 以 12 交接物 §3 对齐）。

## 5. mock 依赖状态（31）

- **31 已提供**（`workers/adapters/mock.py` + `_mock_script.py`，并行交付）。本包已把任务函数
  与 mock 驱动对齐：`run_worker_phase`/`run_conclude_phase` 接受并转发 `phase`/`stage` 给
  `driver.build_execute`（31 mock 用 `mock-phase:`/`mock-stage:` 标记识别阶段；claude/codex/pi
  忽略）。各任务函数传递：bootstrap / bootstrap_conclude / reason / explore_execute /
  explore_conclude / verify（stage=blind|comparison）/ audit。
- `TestMockFullChain`（test_tasks.py）用 **31 MockDriver + 11 LocalBackend（真实子进程）**跑通
  reason / verify 两用例（importorskip 保护；46 skipped 中 1 个仍为历史 31 未就绪时 skip，
  实际 70 passed 已含这两个 mock 用例）。bootstrap→explore 全链路 mock 回归由 50 全量复验
  （verify-mock-test-spec TV-*，涉及 in-process Server + loop，属 40/50 编排）。
- 本包任务函数不感知驱动类型（duck-typing WorkerDriver）；`register_driver("mock", MockDriver)`
  即可被 registry 识别（13 §3 已留接口）。

## 6. 自测结果

- `uv run --project cairn pytest cairn/tests/test_tasks.py -q` → **70 passed**。
- 全量 `uv run --project cairn pytest -q` → **425 passed / 46 skipped**（无失败；46 skipped =
  docker 环境 skip 等环境性跳过）。
- 导入冒烟：`import cairn.dispatcher.{tasks,findings,coverage,replay,progress}` OK；三份 dispatch
  yaml 解析 OK。
- 未 git commit（按编排要求）。
