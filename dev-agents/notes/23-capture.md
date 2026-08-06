# 23-capture 交接物

- 完成 Agent：23-capture  日期：2026-08-06
- 阶段：Phase 1 · 依赖 10（server 基座）。提供 `derive_http_from_capture`（22 依赖）与
  traffic 写回端点（mitmproxy 唯一写入口）。

## 1. 实现清单

| 文件 | 关键符号 | 说明 |
|---|---|---|
| `cairn/src/cairn/server/services/capture.py` | `index_traffic` / `resolve_traffic` / `list_traffic` / `link_finding_traffic` / `get_linked_traffic` / `assert_capture_allowed` / `derive_allow_hosts` / `derive_no_hosts` / `server_assert_capture_allowed` / `derive_http_from_capture` / `reconcile` / `read_capture_gap` / `capture_gap_findings` / `make_digest` / `resolve_client` | 捕获子域服务层。F8 索引、F2 全量/digest、F5 fail-closed、C11 白名单派生、C12 归属、C2 派生与对账 |
| `cairn/src/cairn/server/routers/traffic.py` | `require_capture_token` / `TrafficIndexRequest` / `list_traffic` / `get_traffic` / `index_traffic` | GET 索引/检索、GET 还原（?for_model）、POST 代理回写（F8 受限 token） |
| `cairn/src/cairn/server/middlewares/auth.py` | `default_exempt_paths`（**改动**） | 放行 `POST /engagements/{id}/traffic`（4 段路径精确匹配）；该端点鉴权由路由级 `require_capture_token` 校验 `CAIRN_CAPTURE_TOKEN` |
| `cairn/src/cairn/server/capture_proxy.py` | `CaptureProxyManager` / `CaptureProxyState` / `CaptureWhitelist` / `ProxyEngine`(ABC) / `MitmProxyEngine` / `FakeProxyEngine` / `generate_ca` | 每 engagement 一个 mitmproxy 实例 + 专属 CA + 白名单热刷新 + kill 联动（C3） |
| `cairn/src/cairn/dispatcher/capture/client.py` | `CaptureWhitelist` / `derive_whitelist` / `CaptureClient` / `reconcile_gap` | Dispatcher 侧白名单刷新拉取（经 CairnClient 拉 targets）+ capture_gap 判定辅助 |
| `cairn/tests/test_capture.py` | 21 个测试 | 验收 1-6 + capture spec §10 1-6/11-13/16 |

## 2. 验收关键输出

1. `index_traffic`→`resolve_traffic` 往返字节一致；sha256 校验失败/分片缺失 → `traffic_corrupt`。
2. digest ≤ digest_budget（默认 8192）、截断含 sha256 引用；`for_model=false` 返回全量。
3. fail-closed：白名单外 host / no_capture_hosts（LLM API）→ 服务端 403 SCOPE_DENIED 不落库。
4. C12：`resolve_client(client_ip, ip_to_worker)` bridge 命中 worker；host 网络 → client=NULL 记录 client_ip。
5. C11：targets 增删后白名单 ≤1 interval 刷新生效；kill → 引擎停 + 白名单置空。
6. `cairn serve` 冒烟：/health 200；GET /traffic 无主 token → 401（主 token 保护生效）。

## 3. 写回端点鉴权（F8）

- `POST /engagements/{eid}/traffic` 使用**代理受限 token**（env `CAIRN_CAPTURE_TOKEN`，
  对应 dispatch-config-spec §5 `security.capture_token_env`），**非 Bearer 主 token**。
- 实现方式：`middlewares/auth.py#default_exempt_paths` 对该路径放行（精确匹配
  `method==POST 且 path 恰为 4 段 /engagements/{id}/traffic`），路由级 `require_capture_token`
  校验受限 token（缺 → 401 AUTH_REQUIRED；错 → 401 AUTH_INVALID）。主 token 调用该端点也会
  AUTH_INVALID（严格隔离）。`GET /traffic`、`GET /traffic/{tid}` 仍走主 token（非豁免）。
- 这是对 10 的 `middlewares/auth.py` 的**唯一改动**（加法，不影响原豁免）；编排者注意 25
  的 /projects 豁免与此并存，互不影响。

## 4. digest 格式（F2）

- `make_digest(req_bytes, resp_bytes, sha256, budget=8192)`：
  `--- REQUEST (N bytes) ---` + 请求行 + 全部请求头 + 请求体前缀 2KB + 后缀 512B；随后
  `--- RESPONSE (N bytes) ---` + 响应 status + 全部响应头 + 响应体窗口。截断处标注
  `... [truncated, sha256=<全量校验和>]`。总长超 budget → 整体截断并附 sha256 引用。
- `sha256` = 全量包校验和 `sha256(request ‖ response)`（单列覆盖整包；还原时按此比对，
  不符 → corrupt）。分片（>100MB）按 `xxx.req.{i}` 依序拼接，缺失 → corrupt。
- digest 只喂模型；报告/审计/replay 走 `for_model=false` 全量。

## 5. 白名单刷新机制（C11）

- 服务端 `derive_allow_hosts(conn, eid, scope_policy)`：authorized targets 的 value 规范化
  （url→hostname、domain/ip 直用、cidr 保留）+ scope_policy 显式 allow 叠加；
  `derive_no_hosts` 取 scope_policy.no_capture_hosts（缺省 LLM/Server 默认豁免）。
- 判定 `assert_capture_allowed(host, allow, no)`：host ∈ allow 且 ∉ no；支持 IP∈CIDR 精确匹配，
  域名不子域通配（授权语义）。allow 为空 → 全拒（fail-closed）。
- Dispatcher 侧 `CaptureClient.refresh_whitelist(eid, scope_policy)` 经 `GET /targets` 拉取派生
  （≤ runtime.interval 轮询）；服务端 `CaptureProxyManager.refresh_all(conn)` 为代理内存白名单
  刷新入口。kill/归档 → `whitelist.clear()` + `engine.stop()`（C3）。
- **时序竞态兜底**：白名单未刷新时新资产流量不落盘（fail-closed 默认安全），探索任务用
  `reconcile` 的 capture_gap 标记补测（spec §2.2）。

## 6. 对账阈值来源（C2）

- `reconcile(conn, eid, *, min_capture_ratio=2.0, min_capture_abs_diff=3, ...)`：
  阈值来自 dispatch-config-spec §7 `tuning.min_capture_ratio` / `min_capture_abs_diff`。
- declared = finding_traffic_links 数 + agent_typed http 证据数；captured = traffic_entries 数。
  `declared>0 且 captured==0` 或 `declared ≥ ratio×captured 且 差 ≥ abs_diff` → `capture_gap=true`。
- unverified 占比（代理上报 unverified_count/total_connections）≥ 阈值 → `downgrade_command_evidence`。
- 结果落 `scheduler_state`（key `capture_gap:{eid}`），供 30/40 周期对账 / verify needs_more 判定
  （`read_capture_gap`）。`capture_gap_findings(eid)` 列出「声称有证据但无捕获」的 open/pending_verify finding。

## 7. derive 接口契约（给 22/30/40/41）

```python
derive_http_from_capture(conn, fid, traffic_id, *, traffic_root=None) -> None
```
- **内容**（method/url/request_headers/request_body/response_status/response_headers/
  response_body/note/captured_at/source='captured'/traffic_id）全部取自 traffic 文件字节。
- **登记**走 22 的 `services.findings.add_http_evidence(conn, fid, http_obj=http_obj)`
  （importlib 懒加载；22 已交付，两者已互接）。同时建立 `finding_traffic_links(role='trigger')`。
- **幂等**：同 (finding, traffic_id, source='captured') 去重；同时打断 22 add_http_evidence →
  `_derive_http` 的互递归环路（22 的 add_http_evidence 在 source=captured+traffic_id 时也会调
  derive，我侧 dup 检查兜住）。
- body 内嵌上限 64KB（`HTTP_EVIDENCE_BODY_CAP`），全量在 traffic 文件。
- 给 30/40 的派生调用点：explore 写回时对每个 `traffic_ids` 调 `derive_http_from_capture`；
  22 路由 `POST /findings/{fid}/http` 在 source=captured 时也会自动派生（无需重复调用）。

## 8. proxy 编排状态机（capture_proxy.py）

```
Planning/Active
  start_engagement(conn, eid, scope_policy, ip_to_worker)
    → 读 scope_policy.capture_proxy.port（缺省 8080）
    → generate_ca(eid, ca_dir)（openssl；私钥进程持有，注入容器只给 .pem，文件 0700/C13）
    → 派生 allow/no 白名单 → engine.start(port)
  refresh_all(conn)（≤ interval 轮询）→ 从 targets 重派生白名单
Kill / 归档
  stop_engagement(eid) → engine.stop() + whitelist.clear()（C3）
```
- 引擎抽象 `ProxyEngine`：`MitmProxyEngine`（subprocess mitmdump；mitmproxy 未安装时构造抛
  RuntimeError）+ `FakeProxyEngine`（测试 double）。**当前环境 mitmproxy 不可用** → 用 fake，
  不阻塞；`MITMPROXY_AVAILABLE=False` 已标注。
- C12：Dispatcher 注入 `ip_to_worker`（容器 IP→worker 名），`state.resolve_client(client_ip)` 反查；
  host 网络共享 IP → None（client=NULL 记录 client_ip）。
- CA 注入/容器侧 env（SSL_CERT_FILE/REQUESTS_CA_BUNDLE/NODE_EXTRA_CA_CERTS）由 11 负责，见
  worker-sandbox-hardening §4.1。

## 9. 未实现 / 待定

- **真实 mitmproxy 集成未实现**（环境无该依赖）：`MitmProxyEngine` 已写但未联调；mitmproxy addon
  脚本（把流量写成 traffic 文件 + 调 POST /traffic + fail-closed 判定）未落地，接口已抽象。装
  mitmproxy 后按 `ProxyEngine` 契约补齐。C3 kill 联动已由 `stop_engagement` 覆盖（state 级）。
- **归档（C4）分级迁移/zstd/加密未实现**：`archived`/`archived_path` 列由索引写入保留，物理迁移
  逻辑留给后续（41/运维）。`generate_ca` 已做 0700（C13 at-rest 部分）。
- **no_mitm/WebSocket/gRPC/隧道边界表项**（F10/§3.1）：`traffic_entries` 模型不变，降级标记可由
  `note`/`content_type` 表达；命令证据登记走 22，pcap 落盘未实现（11/13 容器侧）。
- **`finding_http_evidence` 不建不直写**：由 22 登记，本包只派生内容（`derive_http_from_capture`）。

## 10. 给下游的接口

- **给 22**：`capture.derive_http_from_capture(conn, fid, traffic_id, *, traffic_root=None)`；
  `capture.link_finding_traffic(conn, fid, traffic_ids, *, role, source)`（你路由委托）
  `GET /engagements/{eid}/traffic` 检索（client/since/host 过滤）。
- **给 30（verify/explore）**：`capture.get_linked_traffic(fid)`（role/source 原样）；
  `capture.read_capture_gap(eid)` / `capture.capture_gap_findings(eid)`（capture_gap → verify
  needs_more）；`resolve_traffic(eid, tid, for_model=True)` 给 digest；`list_traffic(eid, client=, since=)`
  在派发前注入候选 traffic_ids。
- **给 40（周期对账/调度）**：`capture.reconcile(eid, min_capture_ratio, min_capture_abs_diff)` 周期
  调用入口；`CaptureProxyManager.refresh_all(conn)`（C11 轮询）；kill/归档 → `stop_engagement(eid)`（C3）。
- **给 41（报告）**：`get_linked_traffic(fid)` + `resolve_traffic(eid, tid, for_model=False)` 全量
  （D4：大流量只给引用 traffic_id+sha256+digest，不内嵌 GB 级包）。

## 11. 注意事项

- 路由挂载：`routers/traffic.py` 仅含 traffic 三端点；`POST /engagements/{id}/findings/{fid}/traffic`
  由 22-findings 注册（模块排序先于本模块），其 handler 委托本域的 `link_finding_traffic` ——
  **不要在本模块重复注册**（避免遮蔽死路由）。
- `middlewares/auth.py` 的 F8 豁免是本包唯一改动共享文件；对 GET /traffic 无影响。
- 全量 pytest 现为 **218 passed + 12 failed**；12 个失败全部在 `test_findings.py`（22 并行期
  未完成）：(a) `finding_command_evidence` 无 `created_at` 列但 22 查询 `ORDER BY created_at`；
  (b) 多 engagement 播种 `targets.id` 冲突（`next_id('target', eid)` 按 engagement 计数但
  `targets.id` 为全局 PK，`t-001` 跨 engagement 重复）。与本包无关，未改动 22 代码。
