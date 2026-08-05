# 规则编号注册表（唯一来源 · Rule Registry）

> 配套：全部文档。**任何文档引用规则编号必须以本表为准**；发现新规则必须先在表内登记新编号，禁止复用已有编号。
> 修订历史：2026-08-05 增量复查后建立本表，消除 C3/C4/C5/C6/C7/C8、D1/D5、B2/B3 的「一码多义」。

---

## 1. 编号唯一性原则

- 每个编号 = 一条规则，语义唯一；
- 冲突的次要含义已重编号或并入相邻规则（下表「并入」列）；
- 规则编号在文档正文中以 `（C12）`、`（F2）` 形式引用，测试映射（verify-mock）与本表严格对齐。

---

## 2. A 组：架构/改造约束

| 编号 | 规则 | 出处（原始） |
|---|---|---|
| A1 | `engagement_counters`：engagement 作用域 ID 自增（kind ↔ 前缀映射见 DDL §4.1） | v2 §5.2 |
| A2 | 彻底删除 goal 达成完成判定（`complete` 端点 / project `completed` 态一并移除） | v2 §4.6 |
| A3 | 覆盖口径统一：`compute_gaps` / `pick_audit_targets` / 热力图用同一 `priority_score()` 实时计算，缓存列仅展示 | coverage §2 |
| A4 | ID 前缀 ↔ 计数器 kind 映射统一（DDL §4.1，含 targets/finding_history） | DDL §4.1 |
| A5 | 复测重建复用原行：`retest_round+1` + 状态重置，不新建 item（UNIQUE 下） | coverage §2 |

## 3. B 组：边界/互斥/去重

| 编号 | 规则 | 出处（原始） |
|---|---|---|
| B1 | 覆盖格互斥：`coverage_items.current_intent_id` 认领 + reason 排除 in_progress + 写回校验 | v2 规则38 |
| B2 | `task_runs.project_id` 可空（verify/audit/replay 为 engagement 级任务） | DDL §9.5 |
| B3 | URL/资产规范化去重：去重键 `(engagement,target,title_hash)` 前先归一 scheme/端口/尾斜杠/大小写 | v2 规则39 |
| B4 | `status='not_applicable'` 必须伴 `waivers(kind='not_applicable')`（创建者仅人工） | coverage §1 |
| B5 | 授权窗口到期自动冻结同人工 paused 语义（清 intent claim + reason lease） | v2 §4.2 |
| B6 | workspace 持久卷：每项目工作区跨重启保留，read-only 根下唯一可写 | 原 sandbox B2 |
| B7 | 证据路径映射：容器内 `/home/worker/evidence/<rel>` ↔ `evidence_root/{engagement_id}/<rel>`，相对路径 + 防穿越校验 | 原 v2 §8.12 B3 |

## 4. C 组：捕获/复核/复测（证据链）

| 编号 | 规则 | 出处（原始） |
|---|---|---|
| C1 | 熔断即时性：kill switch 触发即 **SIGKILL**（不走 SIGTERM→grace），不等下一轮 | v2 §4.12 |
| C2 | 捕获字节为准 + 完整性对账：`source='captured'` 由 traffic 派生；`http_mismatch`；`capture_gap` 防「一致地错」 | v2 规则29/40 |
| C3 | kill 即停捕获：熔断/归档同步停止代理与 tcpdump，杜绝 kill 后继续抓包 | v2 规则30 |
| C4 | 捕获配额/归档分级：`capture_quota` 超限滚动归档不删除；Hot/Archive/Destroy 三级 | v2 §2.3 |
| C5 | Agent 凭证最小化：Agent 容器**不持 Cairn token**；仅 Dispatcher/代理持受限写 token | v2 规则37 |
| C6 | CA 语言级信任差异：curl/requests/Go 走 env；Node 走 `NODE_EXTRA_CA_CERTS`；Java 需 trustStore；不信任 → `unverified` | sandbox §4.1 |
| C7 | verify 模型同源性标注：`cross_worker` 同模型族时报告标注「独立性有限」；`verify_model` → `cross_model` | capture §4.1 |
| C8 | reason 空转升级人工：连续校验失败 / finalize 建议被拒超限 → `needs_review`，计数落 scheduler_state | v2 规则41 |
| C9 | 部分覆盖：`tested_scope.partial=1` 热力图半色，不算充分覆盖 | coverage §3.2 |
| C10 | 复测账本：`finding_retest_confirmations` 同轮同类型幂等；`retest_pass≥2 且含≥2 类型` 才允许人工 closed | v2 规则26 |
| C11 | 捕获白名单热刷新：`allow_capture_hosts` 随 targets 增删 ≤1 interval 刷新（原 capture C4-热刷新） | capture §2.2 |
| C12 | 流量归属：`traffic_entries.client_ip` 反查 worker；归属不明 → verify 降级 needs_more（原 capture C3-归属） | capture §2.3 |
| C13 | 捕获数据 at-rest 加密：evidence/traffic 目录静态加密或 0700；归档强制加密（原 capture C6-at-rest） | capture §9.9 |

## 5. D 组：交付/运营/口径

| 编号 | 规则 | 出处（原始） |
|---|---|---|
| D1 | bootstrap `sweep_complete` = 初探完成（绝非项目完成） | prompts §5 |
| D2 | T/H 同一 Bearer Token：服务端不做调用方区分；「仅人工」靠业务规则 + Agent 不持 token 落实 | skeleton §2 |
| D3 | 统一时间线：六源（图/task/finding/traffic/coverage/report）归并只读聚合 | capture §7.4 |
| D4 | 报告证据附录截断策略：内嵌触发包原文（截断阈值内）+ 大流量只给引用（traffic_id+sha256+digest） | v2 §4.10 |
| D5 | `asset_criticality` 来源：按资产类型推断（公网域名/IP、内网段/主机、核心服务上调），人工可覆盖 | v2 §4.13 |
| D6 | 流量容量核算：按真实量级规划 quota（原 capture D5-容量） | capture §8.3 |

## 6. F 组：证据真相源（不变量）

| 编号 | 规则 | 出处（原始） |
|---|---|---|
| F1 | verify 两阶段盲审：blind（独立观察）→ comparison（对照 claim），跳过盲审即校验失败 | v2 规则24 |
| F2 | 证据三层分离：全量文件 / 模型 digest（≤digest_budget，截断含 sha256）/ DB 元数据；**含超大包分片 + 校验和（并入，原 C5-分片）** | capture §8 |
| F3 | 覆盖抽样复核：`audit_runs` 独立重测 + `coverage_discrepancy` 回退重排 | v2 规则34 |
| F4 | 确定性重放复测：原始触发包 + payload 变体 → 响应签名比对；非 HTTP 走**命令确定性重放**（对应物） | v2 规则31 |
| F5 | fail-closed 白名单：`log ⇔ host ∈ allow_capture_hosts ∧ ∉ no_capture_hosts`，白名单外透传不落盘 | v2 规则23 |
| F6 | needs_more 循环上限：`reverify_count > max_reverify` → 升级人工 needs_review | v2 规则28 |
| F7 | verify 独立性部署基线：`require_two_workers`，单 worker 降级 cross_run 且最终人工确认 | capture §4.1 |
| F8 | 代理单写者：代理只写流量文件，索引走 `POST /traffic` 回写；任何进程不得直写 SQLite | v2 规则32 |
| F9 | CLI 结构化流优先 + **防噪声（并入，原 C7-防噪声）**：stdout 含 error 不算 error，仅 stderr/严格签名置红 | capture §7.2 |
| F10 | 协议边界降级（**并入 WebSocket/长连接，原 C8-协议边界**）：pinned TLS/WS/gRPC 降级命令证据，不假装全量 | v2 规则36 |
| F11 | auto_created 闭环：findings 自动建 target 的覆盖项不进 report-ready 口径 | v2 规则33 |

## 7. O 组：运营

| 编号 | 规则 | 出处（原始） |
|---|---|---|
| O1 | 运维手册补足 v2 未含章节（原 ops-runbook D1） | ops §1 |

---

## 8. 重编号对照表（v2 旧引用 → 新编号）

| 旧引用 | 旧含义 | 新编号 | 需修改的文件 |
|---|---|---|---|
| C3 | 流量归属 | **C12** | capture-verify-progress-spec.md |
| C4 | 白名单热刷新 | **C11** | capture-verify-progress-spec.md、ops-runbook.md |
| C5 | 超大包分片/校验和 | **F2**（并入） | v2、capture-verify-progress-spec.md、database-ddl-draft.md |
| C6 | 捕获 at-rest 加密 | **C13** | capture-verify-progress-spec.md、ops-runbook.md |
| C7 | 进度流防噪声 | **F9**（并入） | v2、capture-verify-progress-spec.md、verify-mock-test-spec.md |
| C8 | WebSocket/协议边界 | **F10**（并入） | capture-verify-progress-spec.md |
| D1 | 运维章节补充 | **O1** | ops-runbook.md |
| D5 | 流量容量核算 | **D6** | capture-verify-progress-spec.md、ops-runbook.md |
| B2 | workspace 持久卷 | **B6** | worker-sandbox-hardening.md |
| B3 | evidence 路径映射 | **B7** | v2、worker-sandbox-hardening.md |

> 保持不变的保留编号：A1-A5、B1、B2（project_id 可空）、B3（URL 规范化）、B4、B5、C1、C2、C3（kill 即停）、C4（配额/归档）、C5（Agent 凭证）、C6（CA 语言信任）、C7（模型同源性）、C8（reason 升级）、C9、C10、D1（sweep_complete）、D2-D4、D5（criticality）、F1-F11。
