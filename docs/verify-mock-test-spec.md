# Verify 任务 Mock 驱动用例（全链路回归 · 覆盖 verdict 路径）

> 配套：`capture-verify-progress-spec.md` §4/§5/§6、`backend-module-skeleton.md` §3 TaskType 扩展、`prompts-pentest-templates.md` §4 verify.md
> 目标：用 **mock 驱动（不真调 LLM）** 对「独立复核 Agent」做**确定性回归** —— 覆盖 verdict 三分支 ×（独立性 / 契约 / 异常 / 全链路 / 进度联动），在无真实流量、无 LLM 成本下验证整条 verify 链路
> 基建沿用：`test_mock_end_to_end.py` 的「进程内 Server + DispatcherLoop + LocalBackend」模式

---

## 1. 目标与范围

| 覆盖维度 | 说明 |
|---|---|
| verdict 三分支 | confirmed → verified；rejected → false_positive（先 pending，人工/二次确认后终态）；needs_more_evidence → 回 open + 补证 explore |
| 独立性 | verify 派发 worker ≠ 创建 worker；单 worker 正确降级 |
| 契约校验 | verdict/severity/traffic_ids 三入口非法值 → 拒绝/回退/告警 |
| 异常注入 | 崩溃 / 挂起 / 非 JSON / 空输出 / accepted=false |
| 全链路回归 | bootstrap→explore(出 finding)→自动入队 verify→落定→报告 |
| 进度联动 | task_runs/task_events 生命周期 + SSE 增量续传 |
| 复测重放（规则31/26） | 确定性 replay：重放原始触发包+payload 变体 → 响应签名比对 → `retest_pass`（kind=replay）；仍触发回 open+P0；replay 自身经代理落证据 |
| 捕获字节为准（规则29/32） | agent http[] 与捕获字节不符 → `http_mismatch` + 补证，不得冒充；traffic 索引仅代理写入口 |
| 覆盖闭环（规则28/33/34） | needs_more 循环超限→`needs_review`；auto_created 不阻塞 report-ready；抽样复核 discrepancy 回退 |
| 采集与熔断（规则30/35/36） | 结构化流分类防噪声；kill 即停捕获；协议边界降级命令证据 |

**前置（本期 mock 机制改造，见 §2）**：`verify` 与 `replay` 两个新 phase 加入 `MOCK_ALLOWED_OUTCOMES`；mock explore 契约扩展输出 findings/coverage_result。

---

## 2. 前置：Mock 机制扩展（config.py + mock.py）

### 2.1 `verify` phase 接入现有机制

`dispatcher/config.py`：

```python
MOCK_ALLOWED_OUTCOMES: dict[str, frozenset[str]] = {
    # ...原有 healthcheck/reason/explore_execute/explore_conclude/bootstrap/bootstrap_conclude
    "verify": frozenset({
        "confirmed", "rejected", "needs_more_evidence",
        "accepted_false", "invalid_json", "empty", "command_fail",
    }),
    "replay": frozenset({        # F4：确定性重放（worker='replay-engine'）的结果注入
        "remediated", "unchanged", "ambiguous", "error",
        "invalid_json", "empty", "command_fail",
    }),
}
MOCK_DEFAULT_BEHAVIOR["verify"] = {
    "delay": [0.05, 0.2],
    "outcomes": {"confirmed": "1.0", "rejected": "0.0", "needs_more_evidence": "0.0",
                 "accepted_false": "0.0", "invalid_json": "0.0", "empty": "0.0",
                 "command_fail": "0.0"},
}
MOCK_DEFAULT_BEHAVIOR["replay"] = {
    "delay": [0.05, 0.2],
    "outcomes": {"remediated": "1.0", "unchanged": "0.0", "ambiguous": "0.0",
                 "error": "0.0", "invalid_json": "0.0", "empty": "0.0",
                 "command_fail": "0.0"},
}
```

> `MOCK_ALLOWED_ENV_KEYS` 由 `MOCK_ALLOWED_OUTCOMES` 自动派生 → `MOCK_VERIFY` / `MOCK_REPLAY` 自动合法。
> `replay` 是**确定性引擎任务**（worker='replay-engine'，不走 LLM）——mock 只注入引擎**结果**（response 签名比对产物），用于全链路闭环；引擎内部 `compare_signature` 的真实比对逻辑单测覆盖（§5.2）。

### 2.2 `payload` 字段（verdict 内容注入，向后兼容）

`_parse_mock_phase_payload` 已忽略未知键 → 直接支持 `payload`。mock 脚本 `verify` 分支把 `payload` 合并进输出：

```jsonc
// MOCK_VERIFY 示例 —— 输出 confirmed + 指定定级/流量/动作
{
  "delay": [0.05, 0.2],
  "outcomes": { "confirmed": "1.0", "rejected": "0.0", "needs_more_evidence": "0.0",
                "accepted_false": "0.0", "invalid_json": "0.0", "empty": "0.0",
                "command_fail": "0.0" },
  "payload": {
    "verified_severity": "high",
    "verified_traffic_ids": ["tr-001"],
    "suggested_action": "none",
    "reason": "mock: 原始流量确认注入成立"
  },
  "rules": [
    { "prompt_has": "finding-002", "force": "confirmed",
      "payload": { "verified_severity": "low" } }   // 规则级 payload 覆盖 base
  ]
}
```

mock 脚本新增分支（对齐现有 `if phase=="reason"` 风格）：

```python
if phase == "verify":
    payload = dict(phase_cfg.get("payload") or {})
    # 契约要求 stage=comparison（skeleton validate_verify_compare_payload）；盲审阶段由同任务内
    # 第一次调用模拟（prompt 含 blind 占位符 → 输出 observations），用 rules[].prompt_has 区分两阶段
    if outcome == "confirmed":
        print(json.dumps({"accepted": True,
            "data": {"stage": "comparison", "verdict": "confirmed", **payload}}, ensure_ascii=False))
    elif outcome == "rejected":
        print(json.dumps({"accepted": True,
            "data": {"stage": "comparison", "verdict": "rejected", **payload}}, ensure_ascii=False))
    elif outcome == "needs_more_evidence":
        print(json.dumps({"accepted": True,
            "data": {"stage": "comparison", "verdict": "needs_more_evidence", **payload}}, ensure_ascii=False))
    elif outcome == "accepted_false":
        print(json.dumps({"accepted": False, "reason": "mock_rejected"}, ensure_ascii=False))
    elif outcome == "invalid_json":
        print("{invalid json"); raise SystemExit(0)
    elif outcome == "empty":
        raise SystemExit(0)
    # command_fail → raise SystemExit(1)（复用通用分支）
    raise SystemExit(0)

if phase == "replay":
    payload = dict(phase_cfg.get("payload") or {})
    print(json.dumps({"accepted": True,
        "data": {"result": outcome,
                 "matched_original": payload.get("matched_original", 0),
                 **payload}}, ensure_ascii=False))
    # 重放证据由 writer 关联 replay 生成的 traffic（fixture 预置 tr-101，role=replay）
    raise SystemExit(0)
```

> **新增规则条件 `prompt_has`**：`prompt` 含子串即命中（现有 `fact_ids_gte` 等对 verify 不适用）。`rules[].payload` 与 base `payload` 浅合并 → 测试可用一个 worker 覆盖多场景，无需每用例重建 worker。

### 2.3 mock explore 契约扩展（全链路回归前置）

渗透契约下 explore 输出 findings + coverage_result（见 prompts 模板 §2）。mock `explore_execute` 分支增加 `payload.findings` / `payload.coverage` 输出，供全链路用例（TV-21/22/23）出带 `traffic_ids`/`commands[]` 的 finding：

```jsonc
// MOCK_EXPLORE_EXECUTE 扩展
{
  "delay": [0.05, 0.2],
  "outcomes": { "fact": "1.0", "rejected": "0.0", "invalid_json": "0.0", "command_fail": "0.0" },
  "payload": {
    "findings": [{
      "title": "SQLi in /login", "severity": "high", "cvss_score": 8.1,
      "asset": "http://10.0.0.5:8080/login",
      "traffic_ids": ["tr-001"],
      "http": [{ "method": "POST", "url": "http://10.0.0.5:8080/login",
                 "request_body": "user=admin&pass=' OR 1=1--",
                 "response_status": 200,
                 "response_body": "SQL error near 'OR 1=1'..." }]
    }],
    "coverage": { "covered_items": ["c-013"], "depth_achieved": "standard",
                  "outcome": "finding_created" }
  }
}
```

---

## 3. 测试基建

### 3.1 复用模式（对齐 test_mock_end_to_end.py）

```
进程内 FastAPI Server（TestClient）
    ↕  CairnClient / InProcessClient（加 engagements/findings/traffic/tasks 方法）
DispatcherLoop（LocalBackend + MockDriver，注册 worker-A/B/C）
pytest + httpx 断言
```

### 3.2 注册多个 mock worker（独立性断言的前提）

`dispatcher/config.yaml` 或 fixture 注入：

```python
workers = [
    WorkerConfig(name="worker-A", type="mock", env={"MOCK_VERIFY": _verify_cfg(...), ...}),
    WorkerConfig(name="worker-B", type="mock", env={"MOCK_VERIFY": _verify_cfg(...), ...}),
]
```

- 每个 worker 独立 `MOCK_*` env → 可为 worker-A（创建者）配任意 explore，worker-B/C 配不同 verify 行为
- **独立性断言**基于 `task_runs.worker` 与 finding 的 `detected_by` 比较

### 3.3 关键 fixtures

| fixture | 说明 |
|---|---|
| `server_client` | 新平台 Server TestClient（engagements/findings/traffic/tasks 路由） |
| `engagement` | 创建 engagement（`scope_policy.capture_proxy.enabled=true`）+ 默认测试项目录 + 播种覆盖项 |
| `traffic_seed` | 预置 `traffic_entries`（tr-001/tr-002）模拟代理已捕获 |
| `finding` | 模拟 explore 产物：open 状态 finding + `traffic_ids` 关联（role=trigger） |
| `dispatch` | DispatcherLoop 装配（LocalBackend + workers + in-process client），暴露 `pump_until_idle()` |
| `mock_cfg` | 组装 `MOCK_*` JSON 的辅助（`_phase()`/`_verify(payload, outcomes)`/`_replay(payload, outcomes)`） |
| `replay_seed` | 预置 tr-101（role=replay 证据流量）+ 触发包文件（模拟"重放经代理落证据"）；MOCK_REPLAY 结果注入 |
| `audit_seed` | 预置 high_priority 覆盖项 + worker-B 的 audit 行为（抽样复核用） |

### 3.4 断言辅助

```python
def assert_finding_state(client, fid, *, status, verify_status, severity):
def assert_verified_severity(client, fid) -> str          # verified > agent 生效规则
def assert_worker_exclusion(dispatch, run_id, creator):   # task_runs.worker != creator
def assert_events(dispatch, run_id, *, kinds: set[str]):  # task_events 含这些 kind
def assert_history(client, fid, *notes):                  # finding_history 含审计行
def assert_task_status(dispatch, run_id, status):         # queued/running/success/failed...
def assert_replay_run(client, fid, *, result, matched_original):  # replay_runs 记录
def assert_retest_pass(client, fid, *, kinds):            # retest_pass 及 kind 明细（不同类型确认）
def assert_http_mismatch(client, fid):                    # finding_http_evidence.source/captured + http_mismatch 标记
def assert_audit_run(client, eid, *, item_id, verdict):   # audit_runs 记录（F3）
def assert_no_new_traffic_after(client, eid, ts):         # kill 后无新 traffic 索引（C3）
```

---

## 4. 用例矩阵

### A. 基础 verdict 路径

| ID | 场景 | Mock 配置（MOCK_VERIFY） | 断言 |
|---|---|---|---|
| TV-01 | 确认基础路径 | confirmed + severity=high + traffic_ids=[tr-001] | finding→`verified`；verify_status=confirmed；verified_severity=high；history 增 verify 审计行 |
| TV-02 | 降级定级 | confirmed + severity=low（agent 初判 high） | verified_severity=low 生效；报告标注双轨 `agent=high / verified=low` |
| TV-03 | 升级定级 | confirmed + severity=critical | verified_severity=critical；触发 P0 告警事件（level=error） |
| TV-04 | 拒绝（先落地待确认） | rejected | finding→`pending_false_positive`（**非直接终态**）；verify_status=rejected |
| TV-05 | 拒绝终态（二次确认） | 承接 TV-04 + 人工确认接口 | finding→`false_positive` 终态；history 两条记录（规则引擎落 pending + 人工确认） |
| TV-06 | 证据不足回 open | needs_more_evidence + action=collect_evidence | finding→`open`；自动入队**补证 explore**（同覆盖项）；补证写回后再入 verify 队列；verify_status 重置 none |
| TV-07 | 建议立即复测 | needs_more_evidence + action=retest_now | 直接入队 **retest explore**（非普通补证）；活动面板出现 ⤾ 标签 |
| TV-08 | 确认但流量无交集 | confirmed + traffic_ids=[]（finding 有 tr-001） | 契约层拦截：verified_traffic_ids 与 finding 无交集 → 转 needs_more_evidence 处理 |

### B. 独立性派发

| ID | 场景 | 注册 worker | 断言 |
|---|---|---|---|
| TV-09 | 独立 worker | A(创建者)+B | verify 派给 B；worker-A 的 prompt 流不含 verify 请求 |
| TV-10 | 单 worker 降级 | 仅 A | **不派发**给 A；finding 停留 `pending_verify`；任务标「等待独立复核」；不自动终态 |
| TV-11 | 多 finding 并发 | A 创建 3 个；B/C | 3 个 verify 均匀派到 B/C；**无一派回 A** |
| TV-12 | 复核后再复核幂等 | verified 后人工再触发 | 可再次复核；**重复触发不产生重复任务**（派发去重键：finding_id+stage） |

### C. 契约与异常

| ID | 场景 | 注入 | 断言 |
|---|---|---|---|
| TV-13 | 非法 verdict 值 | payload.verdict=maybe（脚本硬写） | 契约校验拒绝 → 该次 verify `failed`；finding 保持 pending_verify；重试 ≤N |
| TV-14 | 非法 severity | payload.verified_severity=insane | 校验拒绝 → 回退用 `agent_severity`；error 事件记录 |
| TV-15 | 流量引用不存在 | payload.verified_traffic_ids=[tr-999] | 校验拒绝（id 不在 traffic_entries）；不落 verdict |
| TV-16 | accepted=false | outcome=accepted_false | 任务标记 `rejected`；不写 finding 任何字段 |
| TV-17 | 非 JSON / 残缺 | outcome=invalid_json | 重试 ≤3 后 `failed`；finding 保持 pending_verify；人工提醒事件 |
| TV-18 | 空输出 | outcome=empty | 同 TV-17 重试策略 |
| TV-19 | 崩溃 | outcome=command_fail（exit 1） | 任务 `failed`；进入重试队列 |
| TV-20 | needs_more 循环超限（规则28/F6） | MOCK_VERIFY 全 needs_more_evidence，max_reverify=3 | 第 4 次 `reverify_count=4 > 3` → finding `needs_review` 升级人工；**停止自动补证循环**；history 记 4 条补证 + 1 条人工介入提醒；`pending_verify` 不再自动派发 |

### D. 全链路回归（端到端）

| ID | 场景 | 编排 | 断言 |
|---|---|---|---|
| TV-21 | 发现→复核→报告 | bootstrap(mock)→explore(mock) 出 HTTP finding（traffic_ids=[tr-001]）→自动入队 verify(confirmed)→报告 | finding verified；报告含**请求/响应包 + verified_severity**；task_runs 含 verify 记录 |
| TV-22 | 非 HTTP 漏洞链 | explore 出 finding（仅 `commands[]` 证据）→ verify 读命令回显 | verify 以命令回显判 confirmed；报告含命令 + 回显 |
| TV-23 | 复测通过闭环 | finding→fixed→重建覆盖项→retest explore→verify「不再触发」→retest_pass+1 | 两次独立确认后（retest_pass≥2）人工可 `closed`；期间 finding 保持 verified/fixed |
| TV-24 | 复测仍存在 | retest verify confirmed（仍触发） | finding 回 `open`；P0 告警事件 |
| TV-25 | 豁免流量不可用 | finding 引用 tr-XXX 但 traffic_entries 无该记录（no_capture 命中） | writer 打 `traffic_missing` 标记；verify 默认 needs_more_evidence；报告标注证据缺口 |
| TV-26 | 报告幂等 | verify 完成后连续生成两次报告 | 报告一致；不重复计数、retest_pass 不变 |

### E. 进度与联动

| ID | 场景 | 断言 |
|---|---|---|
| TV-27 | verify 运行事件流 | task_runs：queued→running→success；task_events 含 step/tool/command kind；摘要 ≤512B |
| TV-28 | SSE 增量续传 | `GET /tasks/{id}/events?after_seq=N` 只返回 >N 增量；断点续传无丢无重 |
| TV-29 | 前端联动 | verify running 时 `GET /findings?status=pending_verify` 可见；confirmed 后 severity 徽标数据更新 |

### F. 复测重放 · 捕获字节 · 协议边界（v2 §12 规则 26/29/31/32/36 对齐）

| ID | 场景 | Mock 配置 | 断言 |
|---|---|---|---|
| TV-30 | 确定性重放·已修复（规则31/F4） | finding `fixed` + trigger tr-001；MOCK_REPLAY `remediated`（matched_original=0） | replay_runs 记录 result=remediated；`retest_pass(kind=replay)+1`；重放证据 tr-101（role=replay）入 traffic_entries；活动面板 ⤾ 计数 +1 |
| TV-31 | 确定性重放·仍触发（规则31） | MOCK_REPLAY `unchanged` + matched_original=2 | finding 回 `open` + P0 告警（level=error）；**HTTP 类未过 replay 不得人工 closed**（规则26 拦截：replay_runs 为空 → 403）；报告含 replay 重放记录 |
| TV-32 | 重放·响应签名比对（规则31） | 变体注入：status 200 同、body 指纹不同 | `compare_signature` → `ambiguous` → 自动二次 verify；网络错误 → result=error → 有限重试（≤2）后 failed + 人工提醒 |
| TV-33 | 捕获字节为准（规则29/C2） | explore payload.http 与 tr-001 捕获字节不符（agent 手写 response_body） | writer 打 `http_mismatch` 标记 + error 事件；**不落 agent 手写内容为 captured 证据**；按需补证 explore 后重验 |
| TV-34 | 代理单写者（规则32/F8） | 直写 SQLite traffic 的调用（绕过 POST /traffic） | 拒绝/隔离（事务层）；traffic 仅经代理回写入口进入；`traffic.source='proxy'` 断言 |
| TV-35 | 协议边界降级（规则36/F10） | pinned TLS/WS finding（无单 req/resp） | verify 以命令回显判 confirmed；报告标注证据缺口；不伪造 http[]（不假装全量） |

### G. 覆盖闭环 · 熔断与采集（v2 §12 规则 28/30/33/34/35 + 恢复）

| ID | 场景 | Mock 配置 / 注入 | 断言 |
|---|---|---|---|
| TV-36 | auto_created 闭环（规则33/F11） | finding 引用未登记资产（authorized 内） | auto_created target + 覆盖项；report_ready **不阻塞**（该项排除出口径，规则33）；缺口/收敛不计入 |
| TV-37 | 覆盖抽样复核（规则34/F3） | high_priority 格子采样命中；另一 worker 跑 audit | audit_runs 记录；`coverage_discrepancy` → 该项回退 untested + 缺口重排 + 热力图 ⚠；一致 → 保持 tested |
| TV-38 | kill 即停捕获（规则30/C3） | verify 运行中 `POST /kill` | 代理/tcpdump 同步停止（容器断言）；kill 后**无新 traffic 索引**（assert_no_new_traffic_after）；任务 cancelled、不 conclude |
| TV-39 | 结构化流分类（规则35/F9） | CLI 输出 stdout 含 "error"/"timeout" 词 | 不产生 error 事件（scanner 输出）；仅 stderr/严格签名置红；摘要 ≤512B |
| TV-40 | 挂起超时重派（原 TV-20 迁移） | delay=[1200,1200] + verify_timeout=5s | 超时强制取消 → 重派（本轮排除该 worker）；恢复后正常完成 |

> **规则 28-36 → 用例映射**：28→TV-20 · 29→TV-33 · 30→TV-38 · 31→TV-30/31/32 · 32→TV-34 · 33→TV-36 · 34→TV-37 · 35→TV-39 · 36→TV-35。逐条对齐 v2 §12，改动实现时以本表回归。

### H. 修复闭环新增（v2 §12 规则 37-41 / A2·A5·B1·C2·C8·C10）

| ID | 场景 | 注入 | 断言 |
|---|---|---|---|
| TV-41 | 格子互斥（规则38/B1） | 两个 explore intent 引用同一覆盖项 c-001；第二个派发前 `claim_item_for_intent` | 第二个 claim 返回 False **不派发**；已派发的写回时格子被他人认领 → `COVERAGE_ALREADY_COVERED` 作废 + release；无幽灵任务、无重复打格 |
| TV-42 | 捕获完整性对账（规则40/C2） | explore 声明 10 个 `http[]`/`traffic_ids`，traffic_entries 仅 2 条（模拟静默缺抓） | writer 打 `capture_gap` 标记 + error 事件；verify 默认 `needs_more_evidence`；报告证据附录标注「疑似缺抓」 |
| TV-43 | reason 空转升级人工（规则41/C8） | `MOCK_REASON` 连续输出校验失败（无 intent 无 finalize），`max_consecutive_failures=3` | 第 4 次 → engagement 置 reason `needs_review`，停止自动重试；计数落 `scheduler_state`（重启不丢）；人工恢复后可继续 |
| TV-44 | 复测账本幂等与归零（A2/C10） | fixed 后 replay(remediated)+verify(confirmed)+人工签收 各一；随后 replay 重复触发；再注 MOCK_REPLAY=unchanged | `finding_retest_confirmations` 3 行（同轮同 kind 各 1）→ `retest_pass=3`；replay 重复触发**不 +1**；unchanged → finding 回 open + P0 + `retest_pass` 归零 + `retest_round+1`，旧轮确认不继承 |
| TV-45 | 部分覆盖不虚标全绿（C9） | explore 输出 `tested_scope.partial=true`（仅测部分端点） | `coverage_records.partial=1`；热力图摘要 `partial` 计数 +1；reason 仍可列低优先级补测；report-ready 不因 partial 阻塞但明示 |
| TV-46 | 非 HTTP 命令确定性重放（规则26/F4 对应物） | fixed（commands[] 型 finding）→ 命令重放 wrapper 捕获真实回显 | `finding_retest_confirmations(kind='replay')` +1（非 HTTP 确定性通道，杜绝 Agent 自证）；命令重放仍成功（弱口令仍可登录）→ 回 open + P0；未过命令重放门槛人工 closed → 403 |

> **新增规则 37-41 → 用例映射**：37→（派发独立性断言，见 TV-09/11 的 worker 排除 + §4.2 凭证策略）· 38→TV-41 · 39→（去重规范化，见 B3）· 40→TV-42 · 41→TV-43 · C10→TV-44 · C9→TV-45 · 规则26（非 HTTP 确定性通道）→TV-46。

---

## 5. 全链路主流程（TV-21 展开为可执行脚本）

```python
def test_verify_full_chain(server_client, dispatch, engagement, traffic_seed):
    # 1. 播种覆盖项（engagement fixture 已完成）
    # 2. bootstrap → discoveries 播种
    pump_until_idle(dispatch)                       # worker-A 跑 bootstrap
    assert discoveries 已入 coverage_items

    # 3. explore（worker-A 创建 finding，traffic_ids=[tr-001]）
    pump_until_idle(dispatch)
    fid = latest_finding(server_client)
    assert_finding_state(fid, status="open", verify_status="none")

    # 4. 自动入队 verify + 独立性
    run = find_verify_run(dispatch, fid)
    assert_worker_exclusion(dispatch, run.id, creator="worker-A")   # 派给 B/C

    # 5. verify confirmed → 落定
    pump_until_idle(dispatch)
    assert_finding_state(fid, status="verified", verify_status="confirmed")
    assert_verified_severity(fid) == "high"

    # 6. 报告含请求/响应包 + verified_severity
    rpt = render_report(server_client, engagement.id)
    assert "POST http://10.0.0.5:8080/login" in rpt
    assert "SQL error near" in rpt                  # 响应包原文
    assert "verified_severity" in rpt or 双轨标注 present

    # 7. 进度联动
    run_events = get_events(server_client, run.id)
    assert {"step", "tool", "command"} <= {e["kind"] for e in run_events}
```

### 5.2 复测重放主流程（TV-30/31 展开为可执行脚本）

```python
def test_replay_retest_chain(server_client, dispatch, engagement, traffic_seed, replay_seed):
    # 1. explore 出 finding（trigger tr-001）→ verify confirmed → verified
    pump_until_idle(dispatch)
    fid = latest_finding(server_client)
    assert_finding_state(fid, status="verified", verify_status="confirmed")

    # 2. 人工标记 fixed + 重建覆盖项（rebuild_for_retest）
    transition_finding(server_client, fid, to_status="fixed", by="human")
    assert coverage item 已重建（rebuild_for_retest）

    # 3. 自动入队 retest explore → 无发现 → 自动入队 replay（worker='replay-engine'）
    pump_until_idle(dispatch)
    run = find_replay_run(dispatch, fid)
    assert run.worker == "replay-engine"

    # 4. MOCK_REPLAY=remediated → 签名比对通过 → retest_pass(kind=replay)+1
    pump_until_idle(dispatch)
    assert_replay_run(fid, result="remediated", matched_original=0)
    assert_retest_pass(fid, kinds={"verify", "replay"})      # 不同类型确认累计（规则26/规则31）
    assert 重复触发不再 +1（同 kind 幂等，TV-23 断言延伸）

    # 5. 反向路径（TV-31）：MOCK_REPLAY=unchanged → 回 open + P0
    switch_mock(dispatch, "replay", result="unchanged", matched_original=2)
    pump_until_idle(dispatch)
    assert_finding_state(fid, status="open", verify_status="none")
    assert P0 告警事件（level=error）

    # 6. HTTP 类未过 replay 不得人工 closed（规则26/规则31）
    with pytest.raises(403):  transition_finding(fid, to_status="closed", by="human")
```

> replay 引擎的 `compare_signature`（status + body 指纹比对）为纯函数，**单测覆盖**（status 同/异、body 指纹同/异、空 body、超长截断），不依赖 mock。

---

## 6. 执行方式与 CI

```bash
pytest tests/verify -m verify -q          # 仅 verify 用例
pytest tests/verify -m "verify and e2e" -q
pytest tests/ -q                          # 全量回归（含原 mock 链路，不得破坏）
```

CI 三阶段：
1. **单元**：契约校验器（verdict/severity/traffic_ids 三入口）+ `compare_signature` 签名比对 —— 最快、故障定位精确
2. **集成**：Server API（findings 状态机 / task_runs / events 增量）
3. **全链路**：进程内 Server+Dispatcher 端到端（本文件 §4 D/E 组）

**时序确定性**：mock 延迟固定 `[0.01, 0.01]` + `pump_until_idle()` 循环（带超时），避免随机权重抖动；需要特定 verdict 时用 `rules[].force` + `payload`，不依赖概率。

---

## 7. 验收标准

1. **46 用例**（TV-01..46）覆盖 verdict 三分支 ×（正常/独立性/契约/异常/全链路/联动）+ 复测重放/命令确定性重放/捕获字节/覆盖闭环/采集熔断/格子互斥/捕获对账/reason 升级/部分覆盖，全部在无 LLM、无真实流量下运行
2. 每次 verify 派发满足「worker ≠ 创建者」（TV-09/11），单 worker 降级正确（TV-10）
3. 契约校验三入口（verdict/severity/traffic_ids）非法值各有确定性断言（TV-13/14/15）
4. 全链路主流程（§5）一次通过：bootstrap→explore→verify→报告 含请求/响应包 + 命令回显
5. 原 CTF mock 链路（test_mock_end_to_end.py 场景）**不回归**——新增 `verify`/`replay` phase 与 `payload` 均向后兼容
6. v2 §12 规则 28-41 每条有确定性断言（映射见 §4 F/G/H 组表下）
7. 每用例耗时 <5s（mock 快路径），全组 CI <2min
