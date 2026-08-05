# Agent 23 — 捕获子域（Capture / Traffic / Digest / 对账）

> 阶段 1 · 依赖 10。你提供 `derive_http_from_capture`（22 依赖）与 traffic 写回端点（mitmproxy 唯一写入口）。

## 0. 开工前必读
1. `CLAUDE.md`（不变量 1/4/9）
2. `docs/capture-verify-progress-spec.md` §2（透明代理捕获/F5 白名单/C12 归属/C11 热刷新）、§3（非 HTTP + F10 协议边界）、§8（存储/digest/归档）、§9（安全）、§10 验收
3. `docs/database-ddl-draft.md` §9.1（traffic_entries/finding_traffic_links/finding_command_evidence）、§4.1（ID）、§2.1 scope_policy.capture_proxy
4. `docs/backend-module-skeleton.md` §2.5 traffic 路由、§3 capture 服务签名
5. `docs/worker-sandbox-hardening.md` §4.1（CA 注入/语言级差异 C6）
6. `docs/rule-registry.md`（C2/C3/C6/C11/C12/C13/F2/F5/F8/F10）

## 1. 交付范围
```
cairn/src/cairn/server/services/capture.py     # index_traffic / resolve_traffic / link_finding_traffic / assert_capture_allowed / derive_http_from_capture / reconcile / digest
cairn/src/cairn/server/routers/traffic.py      # traffic 索引/检索/还原/关联
cairn/src/cairn/server/capture_proxy.py        # mitmproxy 进程编排（每 engagement 一个）：启动/CA 生成/白名单热刷新/停止
cairn/src/cairn/dispatcher/capture/client.py   # Dispatcher 侧：白名单刷新拉取、capture_gap 判定辅助（也可放 30）
cairn/tests/test_capture.py
```

## 2. 必须满足的契约
- **A. 写入口（F8）**：`POST /engagements/{id}/traffic`（鉴权=代理受限 token，**非 Bearer 主 token**）。`index_traffic` 收 mitmproxy 回写元数据（method/url/host/client_ip/status/req_path/resp_path/bytes/sha256/chunk_count/content_type），落 `traffic_entries`；代理只写文件 + 调本端点，Server 是唯一 DB 写者。
- **B. 归属（C12）**：`client` 由 `client_ip` 反查 worker——每 worker 容器独立 IP（bridge）。归属不明（host 网络共享 IP）→ client=NULL 记录 client_ip；verify 读「独立 worker 流量」需归属明确，否则 needs_more（联动 22）。
- **C. 还原/digest（F2）**：`resolve_traffic(eid, tid, for_model=False)`：全量 → 读文件拼回（分片按 chunk_count 校验 sha256，`traffic_corrupt` 标记）；`for_model=True` → digest（请求行+全部头+体前缀 2KB+后缀 512B，截断标注 `... [truncated, sha256=...]`，≤digest_budget）。digest 只给模型，报告/审计读全量。
- **D. 白名单（F5 fail-closed + C11 热刷新）**：`assert_capture_allowed(host)`：`host ∈ allow_capture_hosts 且 ∉ no_capture_hosts`，否则透传不落盘。`allow_capture_hosts` 由 authorized targets 派生，**随 targets 增删即时刷新**（订阅或 ≤runtime.interval 轮询）；auto_created target 先加白名单再播种；kill/归档置空。
- **E. 派生（C2）**：`derive_http_from_capture(fid, traffic_id)`：以捕获字节派生 `finding_http_evidence(source='captured')`（22 登记，你填充内容）。agent 手写 `http[]` 仅语义注释；verify 阶段比对 `http_mismatch` 逻辑在 30 或 22（比对结果落 verify_runs）。
- **F. 对账（C2 增强）**：`reconcile(eid)`：explore 声明 http[]/traffic_ids 数 vs 时间窗捕获数（`min_capture_ratio`/`min_capture_abs_diff`）→ `capture_gap` 标记（落 finding/coverage_record + error 事件）；`unverified` 占比超阈 → 命令证据降级。周期对账入口供 40 调用。
- **G. 代理进程**：`capture_proxy.py` 每 engagement 一个 mitmproxy 实例：专属 CA（Dispatcher 持私钥，注入容器只给证书）、`port` 从 scope_policy.capture_proxy、归档/停止与 kill 联动（C3：kill 即停抓包）。**11 负责容器侧环境变量，你负责代理侧编排**。at-rest 加密（C13）：evidence/traffic 目录 0700 或加密卷；归档强制加密（C4）。
- **H. 非 HTTP（F10/§3.1）**：tcpdump pcap 落盘（record_pcap）；命令证据由 Agent 上报（22 登记）；协议边界降级表（no_mitm/WebSocket/gRPC/隧道）落 `traffic_entries` 或标记，报告标注证据缺口。

## 3. 验收标准
1. `index_traffic`→`resolve_traffic` 往返：字节一致；sha256 校验失败标 corrupt。
2. digest：≤digest_budget、截断含 sha256 引用；`for_model=false` 返回全量引用。
3. fail-closed：白名单外 host 请求不被索引（模拟代理回写时服务端拒绝/标记）；LLM/Server 域名在 no_capture_hosts。
4. C12：client_ip→worker 映射正确；host 网络 → client=NULL。
5. C11：targets 增删后 `assert_capture_allowed` 行为 ≤1 interval 更新；kill 后白名单置空。
6. 对照 capture spec §10 验收 1-6/11-13/16 自查（写交接物）。

## 4. 硬约束
- 你**不建** traffic_entries 之外的表；`finding_http_evidence` 由 22 登记、你填充 captured 内容——通过服务函数调用，不直写对方表（同事务内可，但签名边界走 22）。
- mitmproxy 若环境不可用（无该依赖），用接口抽象 + 测试用 fake；不阻塞，交接物标注。
- 归档（C4）分级逻辑：hot/archive 迁移 + zstd + 加密；`DELETE engagement` 才清归档。

## 5. 交接物
写 `dev-agents/notes/23-capture.md`：写回端点鉴权、digest 格式、白名单刷新机制、对账阈值来源、derive 接口契约、proxy 编排状态机、未做项。
