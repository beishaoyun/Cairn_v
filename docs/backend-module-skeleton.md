# 新平台后端模块骨架与接口清单

> 配套：`architecture-research-report-pentest-v2.md`（架构）、`database-ddl-draft.md`（数据）
> 用途：从零搭建时按此目录与接口清单组织代码 —— 保留原「Server / Dispatcher」双层与四层分层，新增授权/覆盖/漏洞三子域

---

## 1. 目录结构

```
src/cairn/
├── __init__.py                      # __version__
├── cli.py                           # serve / dispatch 子命令
├── config.py                        # 服务端配置（DB路径/Token/证据根目录/分页）
│
├── server/                          # ══ 协议真相源 ══
│   ├── app.py                       # FastAPI 装配 + 全局异常 handler + 静态/前端
│   ├── db.py                        # 连接管理 + 全量 DDL（见 ddl 草案）+ 迁移
│   ├── errors.py                    # 错误码枚举（AUTH/SCOPE/KILL_SWITCH_ON/OUT_OF_AUTHORIZATION_WINDOW/PROJECT_INACTIVE/LEASE/FINDING/COVERAGE/VALIDATION，与 v2 §7.3 对齐）
│   ├── middlewares/
│   │   └── auth.py                  # Bearer Token 校验 + 可选统一响应注入
│   ├── models.py                    # Pydantic DTO（Request/Response + ORM Row 转换）
│   ├── routers/
│   │   ├── settings.py
│   │   ├── engagements.py           # 生命周期 + kill + finalize
│   │   ├── targets.py               # 范围白/黑名单 CRUD
│   │   ├── projects.py              # 探索图项目管理（保留 + engagement 过滤）
│   │   ├── intents.py               # intent claim/heartbeat/release/conclude
│   │   ├── hints.py
│   │   ├── findings.py              # 漏洞 CRUD/状态流转/证据/历史
│   │   ├── coverage.py              # 矩阵/缺口/豁免/播种
│   │   ├── report.py                # 报告生成/下载
│   │   └── export.py                # 图 YAML/timeline 导出（保留）
│   ├── services/                    # 领域逻辑（无状态函数式，每请求短事务）
│   │   ├── graph.py                 # 探索子域：事实图状态机/租约/超时/校验（迁移自 v1 services.py）
│   │   ├── scope.py                 # 授权子域：Engagement 状态机/窗口/熔断/范围判定
│   │   ├── coverage.py              # 覆盖子域：矩阵/缺口/收敛/report-ready/抽样审计
│   │   ├── findings.py              # 漏洞子域：去重/状态机/审计/证据/verify 落定
│   │   ├── capture.py               # 捕获子域：流量索引/还原(digest)/关联/白名单与豁免
│   │   ├── progress.py              # 进度子域：task_runs/task_events 采集与 SSE
│   │   ├── replay.py                # 重放子域：确定性复测复核（响应签名比对）
│   │   └── report.py                # 报告聚合与渲染
│   └── static/                      # 前端构建产物（Vite dist）
│
├── dispatcher/                      # ══ 调度执行器 ══
│   ├── config.py                    # dispatch.yaml（含 security/scope/tuning 段）
│   ├── protocol/
│   │   └── client.py                # CairnClient（+Token +engagements/findings/coverage 接口）
│   ├── scheduler/
│   │   ├── loop.py                  # 主循环 + guards + 状态落库 + 孤儿清理
│   │   └── worker_select.py
│   ├── tasks/
│   │   ├── bootstrap.py             # 攻击面发现 + 覆盖播种 + 初探
│   │   ├── reason.py                # 缺口驱动收敛（recommend_finalize）
│   │   ├── explore.py               # 覆盖项驱动 + findings 输出
│   │   ├── verify.py                # 独立复核：两阶段盲审（blind→comparison）→ verdict
│   │   ├── audit.py                 # 覆盖抽样复核：独立重测高优先格子
│   │   └── common.py
│   ├── replay/
│   │   └── engine.py                # 确定性重放：原始触发包 + payload 变体 → 响应签名比对
│   ├── findings/
│   │   └── writer.py                # 漏洞落库 + 去重 + 证据挂载（重试）
│   ├── coverage/
│   │   └── writer.py                # coverage_result 校验 + 账本写回 + 复测重建
│   ├── capture/
│   │   └── client.py                # 捕获代理查询/关联（digest 还原 + 白名单校验）
│   └── progress/
│       └── stream.py                # CLI 结构化流解析 + 自由文本兜底 + task_events 摘要
│   ├── runtime/                     # backend/process/heartbeat/cancellation/containers/local_*（保留+加固）
│   ├── workers/                     # base/registry/health/adapters/*（保留 + scope 提示注入）
│   │   └── adapters/mock.py         # MOCK_* 扩展：新增 verify phase（outcomes + payload 注入）
│   └── prompts/
│       ├── default/                 # 渗透场景 prompt（见 prompts 改造文档）
│       └── mock/                    # 结构化 JSON prompt（含 coverage/findings 输出）
```

## 2. API 接口清单（方法 / 路径 / 鉴权 / 说明）

> 鉴权列（**D2 澄清**）：`T`/`H` 均为**同一 Bearer Token**，服务端不做调用方区分；`H` 仅表示"设计上应由人工操作"的语义标注。真正的约束靠**业务规则**落实（如 finding 状态升级仅允许通过人工专用接口/参数），而不是不同凭证。

### 2.1 配置
| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET/PUT | `/settings` | T | 超时/熔断/覆盖策略 |

### 2.2 授权范围
| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET/POST | `/engagements` | T | 列表/创建 |
| GET/PUT/DELETE | `/engagements/{id}` | T | 详情/更新/删除 |
| PUT | `/engagements/{id}/status` | H | planning/active/paused/completed/archived 状态机 |
| POST | `/engagements/{id}/kill` | H | 熔断开关 |
| GET/POST | `/engagements/{id}/targets` | T | 范围目标列表/登记 |
| PUT/DELETE | `/engagements/{id}/targets/{tid}` | H | 修改/移除目标 |

### 2.3 覆盖度
| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/engagements/{id}/coverage` | T | 矩阵+热力图数据 |
| GET/POST | `/engagements/{id}/coverage/items` | T | 覆盖项列表/人工播种 |
| PUT | `/engagements/{id}/coverage/items/{cid}` | H | 调整深度/状态/校准 |
| POST | `/engagements/{id}/coverage/items/{cid}/waive` | H | 人工豁免（kind+reason） |
| GET | `/engagements/{id}/coverage/gaps` | T | 缺口清单（reason 输入） |
| GET | `/engagements/{id}/coverage/audit` | T | 覆盖抽样复核历史（audit_runs） |
| POST | `/engagements/{id}/coverage/items/{cid}/audit` | H | 手动触发/确认抽样复核 |
| POST | `/engagements/{id}/finalize` | H | 人工收尾（校验覆盖策略） |

### 2.4 探索图
| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET/POST | `/projects?engagement_id=` | T | 项目列表（可按 engagement 过滤）/创建 |
| GET/DELETE | `/projects/{pid}` | T | 详情/删除 |
| PUT | `/projects/{pid}/title` / `/status` | T | 标题/状态 |
| POST | `/projects/{pid}/reason/claim|heartbeat|release` | T | 项目级 reason 租约 |
| POST | `/projects/{pid}/intents` | T | 声明 intent |
| POST | `/projects/{pid}/intents/{iid}/heartbeat|release|conclude` | T | intent 租约/结论 |
| POST | `/projects/{pid}/hints` | T | 提示注入 |
| GET | `/projects/{pid}/export?format=yaml|timeline` | T | 图快照/时间线 |
> **A2**：`complete` 端点与 project 层 `completed` 状态已彻底删除；复测走 engagement 层 `completed→active(retest=true)`，不存在 project 层 reopen。

### 2.5 漏洞闭环
| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET/POST | `/engagements/{id}/findings` | T | 列表/登记 |
| GET/PUT | `/engagements/{id}/findings/{fid}` | T | 详情/更新（状态升级仅人工） |
| POST | `/engagements/{id}/findings/{fid}/evidence` | H | 上传证据（白名单） |
| GET/POST | `/engagements/{id}/findings/{fid}/http` | T | 请求/响应包证据列表/登记 |
| GET | `/engagements/{id}/traffic` | T | 捕获流量索引/检索 |
| GET | `/engagements/{id}/traffic/{tid}` | T | 还原原始请求/响应全量（`?for_model=true` → digest，F2） |
| POST | `/engagements/{id}/traffic` | **代理** | 捕获索引回写（代理唯一写入口，受限 token，F8） |
| POST | `/engagements/{id}/findings/{fid}/traffic` | T | 关联流量（role=trigger/verification/replay） |
| GET/POST | `/engagements/{id}/findings/{fid}/commands` | T | 命令回显证据 |
| POST | `/engagements/{id}/findings/{fid}/verify` | H | 人工触发/确认复核 |
| POST | `/engagements/{id}/findings/{fid}/replay` | T | 触发确定性重放（F4） |
| GET | `/engagements/{id}/findings/{fid}/replay` | T | 重放历史（replay_runs） |
| GET | `/engagements/{id}/tasks` | T | 任务列表（活动面板，含 status/worker/duration/event_count/latest_event） |
| GET | `/tasks/{task_run_id}/events` | T | SSE/长轮询进度流（after_seq 增量 + 15s 心跳） |
| POST | `/tasks/{task_run_id}/events/ticket` | T | SSE 一次性 ticket（EventSource 带不了 Header） |
| GET | `/tasks/{task_run_id}/events/{seq}/raw` | T | 事件原始分片文件（懒加载） |
| GET | `/engagements/{id}/findings/{fid}/history` | T | 状态流转审计 |
| GET | `/engagements/{id}/findings?status=&severity=` | T | 过滤查询 |
| GET | `/engagements/{id}/timeline?after_ts=&limit=` | T | 统一时间线（D3：图/task/finding/traffic/coverage/report 六源聚合，报告「方法流程」数据源） |
| GET | `/engagements/{id}/stats` | T | 指标统计（漏洞按 severity 分布 / 覆盖趋势 / 任务成功率） |
| GET | `/engagements/{id}/findings/export?format=csv\|json` | T | 漏洞清单导出（交付物） |
| GET | `/engagements/{id}/coverage/export?format=json` | T | 覆盖矩阵导出（含豁免理由/审计） |

### 2.6 报告
| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/engagements/{id}/report` | H | 生成报告（markdown/html） |
| GET | `/engagements/{id}/report/{rpt_id}` | T | 下载 |

## 3. 服务层接口签名（核心）

```python
# services/graph.py  (探索子域 · 权威规格见 exploration-graph-spec.md §3)
def create_project(conn, *, engagement_id, title, bootstrap_enabled=True) -> Project      # 播种 origin/goal 特殊事实
def get_project(conn, pid) -> Project | None
def list_projects(conn, *, engagement_id=None, status=None) -> list[Project]
def delete_project(conn, pid) -> None
def set_project_title(conn, pid, title) -> Project
def set_project_status(conn, pid, status) -> Project                                      # active|stopped（A2 无 completed）
def next_scoped_id(conn, pid, kind) -> str                                               # f###/i###/h###（scoped_counters）
def create_fact(conn, pid, *, description) -> Fact                                       # 只增不改，重复幂等
def create_intent(conn, pid, *, description, creator, from_fact_ids, to_fact_id=None) -> Intent   # goal 禁 from/to、worker∈{null,creator}
def claim_intent(conn, pid, iid, *, worker) -> Intent                                    # 403 非 active / 409 他人持有
def heartbeat_intent(conn, pid, iid, *, worker) -> Intent
def release_intent(conn, pid, iid, *, worker) -> None
def conclude_intent(conn, pid, iid, *, worker, facts=None) -> Intent                     # 写事实 + concluded_at + 释放（coverage/findings 由路由编排 21/22）
def create_hint(conn, pid, *, content, creator) -> Hint
def claim_reason(conn, pid, *, worker) -> None                                           # 项目级租约（reason_* 列）
def heartbeat_reason(conn, pid, *, worker) -> None
def release_reason(conn, pid, *, worker) -> None
def freeze_project_leases(conn, pid) -> None                                             # B5：清 open intent worker + reason 租约
def intent_timeout_cleanup(conn, pid=None) -> list[str]                                  # 读时清理
def reason_timeout_cleanup(conn, pid=None) -> None
def export_graph_yaml(conn, pid) -> str
def list_facts(conn, pid, *, after_ts=None, limit=200) -> list[Fact]
def list_intents(conn, pid, *, open_only=True) -> list[Intent]
def list_hints(conn, pid) -> list[Hint]

# services/scope.py
def create_engagement(conn, *, title, window_start, window_end, scope_policy) -> Engagement
def transition_status(conn, eid, new_status, *, retest=False) -> Engagement      # 状态机校验
def check_engagement_writable(conn, eid) -> None                                  # 403/409
def check_scope_allowed(conn, eid, target_value) -> Target | None                 # SCOPE_DENIED
def check_kill_switch(conn, eid) -> None                                          # 423
def expire_engagements(conn) -> None                                              # 窗口到期自动 pause

# services/coverage.py
def compute_gaps(conn, eid, *, threshold, exclude_in_progress=False, limit=50) -> list[dict]   # B1：reason 消费用 exclude_in_progress=True；limit 防缺口撑爆 prompt
def coverage_summary(conn, eid) -> dict                              # C9：含 partial 计数
def report_ready(conn, eid, policy) -> tuple[bool, dict]
def upsert_coverage_item(conn, eid, target_id, test_type_id, depth, *, seed_source) -> CoverageItem
def claim_item_for_intent(conn, item_id, intent_id) -> bool          # B1：格子互斥（置 in_progress+current_intent_id，已被认领返回 False）
def release_item_for_intent(conn, item_id, intent_id) -> None        # B1：仅 current_intent_id==intent_id 才回退 untested
def write_coverage_result(conn, eid, *, item_ids, depth_achieved, outcome, fact_id, intent_id,
                          evidence_refs, tested_scope=None, partial=False) -> None   # C9：tested_scope/partial 落 coverage_records
def waive_item(conn, eid, item_id, *, kind, reason, by) -> Waiver
def rebuild_for_retest(conn, eid, target_id, test_type_id) -> CoverageItem  # A5：复用原行 retest_round+1 + 状态重置（UNIQUE 下不新建）
def sample_audit(conn, eid, policy) -> list[AuditRun]                # F3：抽样（A3：实时 priority 口径）+ 异常触发派发
def apply_audit_verdict(conn, audit_id, *, verdict) -> None          # F3：discrepancy → item 回退 untested + 缺口重排
def closure_rule(conn, eid, item) -> bool                            # F11：auto_created 目标覆盖项是否阻塞 report_ready
def reason_escalation_state(conn, eid) -> bool                       # C8：连续失败/finalize 被拒超限 → 升级 needs_review

# services/findings.py
def dedup_key(engagement_id, target_id, title) -> str
def resolve_target(conn, eid, asset, *, scope) -> Target | None                     # B1：scope 校验 + auto_created 建 target
def create_finding(conn, eid, *, payload, detected_by, actor='agent') -> Finding   # agent 只能 open
def transition_finding(conn, fid, *, to_status, note, actor) -> Finding            # 人工
def attach_evidence(conn, fid, *, kind, path, mime, size) -> Evidence
def add_http_evidence(conn, fid, *, http_obj) -> HttpEvidence                      # 请求/响应包（同事务；captured 时由 derive 填充）
def triaged(conn, eid) -> int                                                      # 未分诊计数（open/pending_verify/pending_false_positive/needs_review；verified 已分诊，不阻塞 finalize）
def apply_verify_runs(conn, fid, *, vr) -> None                                   # F1：verdict 落定 + verified_severity + verify_status
def bump_reverify(conn, fid) -> bool                                              # F6：reverify_count+1，返回是否超 max_reverify（→needs_review）
def record_retest_confirmation(conn, fid, *, kind, note, actor) -> None           # A2/C10：写 finding_retest_confirmations（同轮同 kind 幂等）+ 刷新 retest_pass
def retest_pass_count(conn, fid) -> int                                           # A2/C10：当前 retest_round 下确认账本行数（含 kind 明细）

# services/capture.py
def index_traffic(conn, eid, *, entry: dict) -> TrafficEntry        # 代理回写索引（代理唯一写入口）
def resolve_traffic(conn, eid, traffic_id, *, for_model=False)      # 全量 或 digest（≤digest_budget，截断含 sha256）
def link_finding_traffic(conn, fid, traffic_ids, *, role, source) -> None   # role: trigger/verification/replay
def assert_capture_allowed(host) -> bool                            # F5：host ∈ allow_capture_hosts 且 ∉ no_capture_hosts
def derive_http_from_capture(conn, fid, traffic_id) -> None         # C2：以捕获字节派生 finding_http_evidence

# services/replay.py  (F4 确定性复测)
def replay_finding(conn, fid, *, variants: list[str]) -> ReplayRun  # 重放原始触发包 + payload 变体
def compare_signature(now_resp, orig_resp) -> SignatureMatch        # status + body 指纹比对
def retest_pass_increment(conn, fid, *, kind) -> None               # 与 services/findings.record_retest_confirmation 同一操作（同轮同 kind 幂等）；统一实现，本签名保留兼容调用

# services/progress.py
def open_task_run(conn, *, engagement_id, project_id=None, task_type, worker) -> TaskRun  # B2：project_id 可空（verify/audit/replay 为 engagement 级）
def append_event(conn, run_id, *, kind, level, message, raw_path) -> None
def events_after(conn, run_id, after_seq) -> list[dict]             # SSE 轮询
def engagement_timeline(conn, eid, *, after_ts=None, limit=200) -> list[dict]   # D3：六源聚合统一时间线

# services/report.py
def aggregate(conn, eid) -> ReportData        # 执行摘要/范围/方法(时间线渲染)/漏洞/覆盖/证据(请求响应+命令回显)
# D4 证据附录策略：内嵌触发请求/响应原文 + 命令回显 + 复核/重放记录；大流量只给引用（traffic_id+sha256+digest），按需还原，报告不内嵌 GB 级原始包
def render_markdown(data: ReportData) -> str
def render_html(data: ReportData) -> str
```

> **TaskType 扩展**：`dispatcher/config.py` 的 `TaskType` Literal 由 `bootstrap|reason|explore` 扩展为 `bootstrap|reason|explore|verify|audit`（LLM 任务，worker 驱动）+ `replay`（**确定性引擎任务**，worker='replay-engine'，不走 LLM）；`WorkerConfig.task_types` 校验同步放宽；verify 派发规则（排除创建者、两阶段盲审、max_reverify）见 capture-verify-progress-spec §4。
> **Mock 扩展**：`MOCK_ALLOWED_OUTCOMES` 增加 `verify` phase（confirmed/rejected/needs_more_evidence/accepted_false/invalid_json/empty/command_fail）+ `payload` 字段注入 verdict 内容（verified_severity/verified_traffic_ids/suggested_action/reason），规则新增 `prompt_has` 条件 —— 全链路 mock 回归用例见 `docs/verify-mock-test-spec.md`。

## 4. 校验器清单（Dispatcher 侧）

| 校验器 | 输入 | 输出/失败 |
|---|---|---|
| `validate_reason_payload` | reason stdout JSON | intents(引用覆盖项) / coverage / rejected |
| `validate_explore_payload` | explore stdout JSON | description + findings[] + coverage_result |
| `validate_findings_payload` | findings 数组 | severity/cvss/cwe/evidence_refs 白名单 |
| `validate_coverage_result` | coverage_result | covered_items∈engagement / 未覆盖 / outcome 枚举 |
| `validate_bootstrap_payload` | bootstrap stdout JSON | fact+complete+discoveries |
| `validate_verify_blind_payload` | 阶段一 JSON | observations 非空数组 / traffic_note |
| `validate_verify_compare_payload` | 阶段二 JSON | stage=comparison + verdict/verified_severity/reason/traffic_ids/http_mismatch |
| `validate_replay_result` | replay 引擎结果 | matched_original / result ∈ {unchanged,remediated,ambiguous,error} |

## 5. 关键改动点对照（相对 v1 目录）

| v1 文件 | v2 归属 | 变更 |
|---|---|---|
| `server/services.py` | `server/services/graph.py` | 拆出授权/覆盖/漏洞三子域 |
| `dispatcher/tasks/reason.py` | 同 | 注入 gaps、输出 coverage、禁 complete |
| `dispatcher/tasks/explore.py` | 同 | 必填 coverage_result + findings |
| `dispatcher/tasks/bootstrap.py` | 同 | 输出 discoveries 播种 |
| `runtime/containers.py` | 同 | 孤儿清理接线 + 资源限制 + 非 root 镜像 |
| `protocol/client.py` | 同 | +Token +engagements/findings/coverage |
| 无 | `server/middlewares/auth.py` | 新增鉴权 |
| 无 | `dispatcher/coverage/writer.py` | 新增覆盖写回（B1：claim/release 格子互斥；A5：复测重建复用原行） |
| 无 | `server/services/timeline.py` | 新增统一时间线聚合（D3） |
| 无 | `dispatcher/findings/retest.py` | 新增复测确认账本（A2/C10：finding_retest_confirmations） |
| 无 | `server/services/capture.py#reconcile` | 新增捕获完整性对账（C2） |
| 无 | `server/services/coverage.py#reason_escalation` | 新增 reason 空转升级人工（C8） |
