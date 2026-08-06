# Phase 2 P2 清理交接物

- 完成 Agent：P2-cleanup  日期：2026-08-06
- 依据：`50-reviewer.md` §2 P2 清单（P2-1..P2-10）+ `phase0-alignment.md` #41-51 登记。
- 基线：517 passed / 0 failed / 0 skipped → 处理后 **520 passed / 0 failed / 0 skipped**（+3 来自 P2-4 新增断言）。
- 范围：docs/ 同步（P2-1/2/3/5/8/9）+ 低风险代码守卫（P2-4，含测试）+ 只评估不实现（P2-10）。未 git commit。

---

## 逐项处理结果

### P2-1 [resolved] FTS5 contentless 不一致（coverage spec §1 vs DDL §8 / db.py:489-491）

- 核对：DDL §8 与 `server/db.py` SCHEMA_INDEXES_DDL（L489-491）均为 `fts_coverage(item_id UNINDEXED, target_value, test_type_name)`，**无 `content=''`**（非 contentless）。coverage spec §1 此前写 `content=''`（contentless）+ 注释声称「无 UPDATE/DELETE，只能 INSERT OR REPLACE」。CLAUDE.md §2 规定 DDL 为唯一权威、db.py 跟随 DDL —— 因此以 DDL/db.py 为口径，**改 coverage spec**。
- diff（旧→新）：`docs/coverage-engine-implementation-spec.md` §1 FTS5 块
  - 旧：`CREATE VIRTUAL TABLE ... fts5(item_id UNINDEXED, target_value, test_type_name, content='')` + 注释「content='' 为 contentless 模式：无 UPDATE/DELETE，只能 INSERT OR REPLACE」
  - 新：`CREATE VIRTUAL TABLE ... fts5(item_id UNINDEXED, target_value, test_type_name)` + 注释「与 DDL §8 / server/db.py 口径一致：非 contentless（无 content=''），常规 FTS5 表，建表即可 INSERT/UPDATE/DELETE；FTS 同步触发器未实现（占位表）」
- 归属：文档（coverage spec）。FTS 同步触发器本身仍属未实现项（phase0 #51，与各包交接一致）。

### P2-2 [resolved] graph spec VALIDATION 状态码 400 → 422

- 核对：`server/errors.py` `ErrorCode.VALIDATION.http_status = 422`；`app.py` CairnError handler 返回 `exc.error_code.http_status`；`test_graph.py` L423-437 断言 `from 含 goal / worker≠creator / to_fact_id=goal` → **422 VALIDATION**。图路由全部按此。
- diff（旧→新）：`docs/exploration-graph-spec.md` 全篇 VALIDATION 引用
  - §2.1「禁止 goal 作 from 源」：400 → 422
  - §2.3「worker∈{null,creator}」：400 → 422
  - §2.4 错误码表 `VALIDATION | 400` → `VALIDATION | 422`
  - §4 规则 1（goal from 源）→ 422；规则 2（worker≠creator）→ 422
  - §5 路由表 intents POST「校验 400/404」→「校验 422/404」
  - §7 验收点 2「from 含 goal → 400；worker≠creator → 400；to_fact_id=goal → 400」→ 全部 422
- 归属：文档（graph spec，对齐 v2 §7.3 / errors.py / test_graph.py）。

### P2-3 [resolved] skeleton §3 两处签名未同步

- 核对实现（`server/services/coverage.py` L701 / `server/services/findings.py` L758）：
  - `apply_audit_verdict(conn, eid, *, item_id, verdict, auditor, reason='sampling', depth_reached=None, note=None) -> audit_runs row`（创建 + 落定一步式）
  - `retest_pass_count(conn, fid) -> dict`（`{retest_round, count, details:[{kind,note,actor,created_at}]}`）
- diff（旧→新）：`docs/backend-module-skeleton.md` §3
  - `apply_audit_verdict(conn, audit_id, *, verdict) -> None` → `apply_audit_verdict(conn, eid, *, item_id, verdict, auditor, reason='sampling', depth_reached=None, note=None) -> AuditRun`（注释补「无两阶段 confirm_audit_run」）
  - `retest_pass_count(conn, fid) -> int` → `retest_pass_count(conn, fid) -> dict`（注释补返回明细 + 调用方用 `["count"]` 取行数）
- 归属：文档（skeleton §3 同步到实现；21/22 交接物 §8 已先登记偏离）。同步 phase0 #21/#22。

### P2-4 [resolved]「capture 必须 bridge」无运行时代码强制 → 加运行时守卫

- 核对：`runtime/containers.py` 此前 `_run_kwargs` 直接用 `self._network_mode`（L393），capture enabled + host 不会报错；`dispatch-config-spec.md` §8 仅注释。判断 capture 开启依据：per-engagement `scope_policy.capture_proxy.enabled`（DDL §2.1 / dispatch-config-spec §8，经 `scope_resolver` 解析进 `ContainerScope.capture_proxy`）。
- 代码改动：`cairn/src/cairn/dispatcher/runtime/containers.py` `_run_kwargs` 顶部新增守卫：
  ```
  if self._network_mode == "host":
      cap = scope.capture_proxy or {}
      if cap.get("enabled"):
          raise ContainerBackendError("capture 模式必须 bridge 网络（C12 / dispatch-config-spec §8）：...")
  ```
  触发点 = `ensure_running` 创建容器（`_run_kwargs` 仅新建时调用），即「启动拒绝」。
- 测试新增（`cairn/tests/test_container_archives.py`，+3，全部通过）：
  1. `test_ensure_running_rejects_host_network_when_capture_enabled`：capture 开 + host → `ContainerBackendError`，且 `run_calls == []`（未创建容器）
  2. `test_ensure_running_host_network_ok_when_capture_disabled`：capture 关 + host → 正常（`network_mode == "host"`）
  3. `test_ensure_running_bridge_ok_when_capture_enabled`：capture 开 + bridge → 正常（默认合规路径）
- 归属：11（runtime/containers.py + test_container_archives.py）。改动小，不触其他包逻辑。既有 `test_ensure_running_host_network_allowed_only_explicitly`（无 capture scope）仍绿——host 仅当 capture 关闭时放行。

### P2-5 [resolved] evidence 上传端点 H 标注与实现语义不符

- 核对：`routers/findings.py` evidence 端点实现为 **JSON+base64**（无 python-multipart，遵守「不引入未明示依赖」），白名单 image/*、text/*、application/pdf + 路径防穿越；Agent 写回（explore evidence_refs 落盘）亦走此端点。D2 下 `H` 仅语义标注（服务端单 token 无法区分调用方）。
- diff（旧→新）：`docs/backend-module-skeleton.md` §2.5
  - evidence 行说明「上传证据（白名单）」→「上传证据（JSON+base64，无 multipart；白名单 image/*、text/*、application/pdf + 路径防穿越）」
  - 表格后新增注释段「evidence 端点实现说明（P2-5）」：实现为 JSON+base64、未引入 python-multipart；`H` 仅表示「设计上应由人工操作」的语义，Agent 写回亦走此端点可达；实际约束由业务白名单（evidence_refs 相对路径 + 文件存在性校验）落实，而非凭证区分。
- 归属：文档（skeleton §2.5）。

### P2-6 [resolved — 确认项，无改动]

- F8 代理单写者端点：`POST /engagements/{eid}/traffic` 豁免主 token 中间件 + 路由级 `require_capture_token` 校验受限 token —— 50-reviewer 已确认无问题。本次无需处理。

### P2-7 [env — 不做]

- Docker 真实镜像构建/运行留待有权限环境（本环境 docker CLI Permission denied）。容器加固仅 fake-client/CLI 单测覆盖（test_container_archives 35 passed）。非本 agent 范围。

### P2-8 [resolved] 容器名前缀偏离

- 核对：`containers.py::container_name` 返回 `f"cairn-{project_id}"`（L292）；graph spec §4-15 原写 `cairn-dispatch-<project_id 的 / → ->`。`proj_###` 不含 `/`，故 `/ → ->` 替换失效，前缀偏离仅为命名问题、无唯一性影响。按「改动最小化 + 代码改动仅限 P2-4」约束，选择**文档对齐到代码**（不触碰 containers.py 与依赖其名字的 12+ 处测试）。
- diff（旧→新）：`docs/exploration-graph-spec.md` §4-15
  - 旧：`容器名 cairn-dispatch-<project_id 的 / → ->`
  - 新：`容器名 cairn-{project_id}`（`dispatcher/runtime/containers.py` 实现；早期约定 `cairn-dispatch-<project_id 的 / → ->` 的 `/ → ->` 替换因 `proj_###` 不含 `/` 而失效，统一采用更短前缀 `cairn-{project_id}`，唯一性无碍）
- 归属：文档（graph spec §4-15）。

### P2-9 [resolved] skeleton §3 open_task_run 参数顺序

- 核对：服务实现 `open_task_run(conn, *, engagement_id, project_id=None, task_type, worker, status='queued', started_at=None, outcome_note=None) -> dict`（`server/services/progress.py` L140）；12 客户端关键字顺序 `open_task_run(eid, task_type=..., worker=..., project_id=...)`。**全部 keyword-only**，顺序无关、语义一致，无功能问题。
- diff（旧→新）：`docs/backend-module-skeleton.md` §3
  - 旧：`open_task_run(conn, *, engagement_id, project_id=None, task_type, worker) -> TaskRun`
  - 新：`open_task_run(conn, *, engagement_id, project_id=None, task_type, worker, status='queued', started_at=None, outcome_note=None) -> TaskRun`（注释补「全部 keyword-only，客户端关键字顺序 ... 与之语义一致，参数顺序无关」）
- 归属：文档（skeleton §3）。

### P2-10 [evaluated — 不改 DDL，给加列建议]

只评估，不改 DDL（`database-ddl-draft.md` §9.3 / `server/db.py`）。涉及实现位置：`server/services/report.py` L192-196（replay_runs ORDER BY started_at）、L899-906（verify_runs 经 findings JOIN 归属 stats）。

**加列建议（影响面 / 迁移 / 涉及包）**：

1. `replay_runs.created_at`（建议加）
   - 现状：报告按 `started_at` 排序；`started_at` 可空（queued 未启动时 NULL → SQLite ORDER BY 升序 NULL 排最前，队列序失真）。有 `created_at` 可给出稳定的「登记时间」排序与队列时长统计。
   - DDL：`ALTER TABLE replay_runs ADD COLUMN created_at TEXT`（§9.3 + `server/db.py` SCHEMA_TABLES_DDL + DDL §10 迁移补列清单）。
   - 回填：`UPDATE replay_runs SET created_at = COALESCE(started_at, finished_at)`（迁移脚本）。
   - 写入方：30 `replay/engine.py` 登记行时补 `created_at`（现 DDL 无此列）。
   - 消费方：41 `report.py` L196 `ORDER BY started_at` → `ORDER BY created_at`（可选）。
   - 涉及包：10（db.py/DDL）、30（replay 登记）、41（report 排序）。低优先，阶段 3 可排。

2. `verify_runs.engagement_id`（建议加，可选）
   - 现状：stats 经 `JOIN findings f ON f.id = vr.finding_id WHERE f.engagement_id=?`（report.py L903-906）归属，功能正确、仅多一次 JOIN。加列可免 JOIN 且便于 engagement 级直接检索/清理。
   - DDL：`ALTER TABLE verify_runs ADD COLUMN engagement_id TEXT REFERENCES engagements(id)`（§9.2 + db.py + DDL §10 迁移）。
   - 回填：`UPDATE verify_runs SET engagement_id = (SELECT engagement_id FROM findings f WHERE f.id = verify_runs.finding_id)`。
   - 写入方：22 `findings.apply_verify_runs`（INSERT verify_runs 时补列）。
   - 消费方：41 `report.py` stats 查询可改用直接列（也可保持 JOIN，兼容旧库）。
   - 涉及包：10（db.py/DDL）、22（apply_verify_runs）、41（report stats）。低优先，阶段 3 可排。
   - 权衡：denormalization（冗余 engagement_id 与 findings 存同值），需保证 findings.engagement_id 变更时同步（当前 findings.engagement_id 实际不可变，无级联改 eid 路径，故风险低）。

**结论**：两项均属「报告级查询更顺」的增强，非缺陷；当前实现（started_at 排序 / JOIN 归属）功能正确。不阻塞交付，列为 phase0 #32/#50 的 phase 3 可选优化。

---

## 测试与断言更新

- 全量 `uv run --project cairn pytest -q` → **520 passed / 0 failed / 0 skipped**（基线 517，+3 为 P2-4 新断言）。
- 无既有测试断言被改动；仅新增 3 例（见 P2-4）。P2-2 的 422 语义与 `test_graph.py` 既有断言一致（未改）。

## 归属汇总

| P2 | 类型 | 归属 | 状态 |
|---|---|---|---|
| P2-1 | 文档 | coverage spec §1（对齐 DDL/db.py） | resolved |
| P2-2 | 文档 | graph spec §2.4/§2.1/§2.3/§4/§5/§7 | resolved |
| P2-3 | 文档 | skeleton §3（apply_audit_verdict / retest_pass_count） | resolved |
| P2-4 | 代码+测试 | containers.py `_run_kwargs` 守卫 + test_container_archives.py +3 | resolved |
| P2-5 | 文档 | skeleton §2.5 evidence 说明 | resolved |
| P2-6 | 确认 | — | resolved（无改动） |
| P2-7 | env | — | 留待有权限环境 |
| P2-8 | 文档 | graph spec §4-15（对齐代码 `cairn-{project_id}`） | resolved |
| P2-9 | 文档 | skeleton §3 open_task_run 注释 | resolved |
| P2-10 | 评估 | 不改 DDL；建议 replay_runs.created_at / verify_runs.engagement_id（见上） | evaluated |
